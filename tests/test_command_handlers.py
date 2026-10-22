"""Exécution des commandes avec une fausse interaction Discord (aucune connexion réseau)."""

from __future__ import annotations

import asyncio

import discord
import pytest
from discord import app_commands

from src.config import Settings
from src.database.connection import MIGRATIONS
from src.discord_bot import bot as bot_module
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.services import design
from src.services.content import Announcement

GUILD_ID = 42


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.modal = None
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self._done = True

    async def defer(self, **kwargs):
        self._done = True

    async def send_modal(self, modal):
        self.modal = modal
        self._done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeUser:
    id = 7
    display_name = "Alice"

    def __str__(self) -> str:
        return "alice"


class FakeGuild:
    id = GUILD_ID
    categories: list = []
    channels: list = []
    roles: list = []
    me = None
    default_role = None

    def get_channel(self, channel_id):
        return None


class FakeInteraction:
    def __init__(self, client) -> None:
        self.client = client
        self.user = FakeUser()
        self.guild = FakeGuild()
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = None
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


@pytest.fixture
async def bot(db):
    instance = SaeBot(Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID)}, require_token=False), db)
    for extension in EXTENSIONS:
        await instance.load_extension(extension)
    yield instance
    await instance.close()


def cog(bot, name):
    return bot.get_cog(name)


def embed_text(embed: discord.Embed) -> str:
    return " ".join([embed.title or "", embed.description or "", *[f.name + " " + f.value for f in embed.fields]])


async def test_help_hides_admin_commands_from_members(bot):
    interaction = FakeInteraction(bot)
    general = cog(bot, "GeneralCog")
    await general.help.callback(general, interaction)
    sent = interaction.response.sent[0]
    assert sent["ephemeral"]
    text = embed_text(sent["embed"])
    assert "/status" in text and "/project" in text
    assert "/setup" not in text and "Administrer" not in text


async def test_status_works_without_discord_connection_or_ai(bot):
    interaction = FakeInteraction(bot)
    general = cog(bot, "GeneralCog")
    await general.status.callback(general, interaction)
    embed = interaction.response.sent[0]["embed"]
    text = embed_text(embed)
    assert f"SQLite répond (schéma v{len(MIGRATIONS)})" in text
    assert "IA générative désactivée" in text
    assert "aucune depuis le démarrage" in text
    assert "pas une surveillance continue" in embed.footer.text


async def test_status_shows_last_error_without_traceback(bot):
    bot.errors.record("src.gitlab", "envoi impossible\nTraceback (most recent call last):")
    interaction = FakeInteraction(bot)
    general = cog(bot, "GeneralCog")
    await general.status.callback(general, interaction)
    text = embed_text(interaction.response.sent[0]["embed"])
    assert "envoi impossible" in text and "Traceback" not in text


async def test_project_without_gitlab_data_invents_nothing(bot):
    interaction = FakeInteraction(bot)
    general = cog(bot, "GeneralCog")
    await general.project.callback(general, interaction)
    text = embed_text(interaction.response.sent[0]["embed"])
    assert "inconnue (aucun webhook reçu)" in text
    assert "aucun événement enregistré" in text
    assert "https://" not in text  # aucun lien n'est configuré dans config/links.toml


async def test_refresh_before_setup_is_refused(bot):
    interaction = FakeInteraction(bot)
    admin = cog(bot, "AdminCog")
    await admin.refresh.callback(admin, interaction, None)
    assert "/setup" in interaction.response.sent[0]["content"]


async def test_admin_messages_and_history(bot):
    admin = cog(bot, "AdminCog")
    interaction = FakeInteraction(bot)
    await admin.admin_messages.callback(admin, interaction)
    assert "liens à compléter" in embed_text(interaction.response.sent[0]["embed"])
    bot.admin_log.record(1, "alice", "setup", "", "ok")
    interaction = FakeInteraction(bot)
    await admin.admin_history.callback(admin, interaction)
    assert "alice" in embed_text(interaction.response.sent[0]["embed"])


async def test_announce_previews_then_saves_only_after_confirmation(bot):
    bot.mark_setup_confirmed()
    admin = cog(bot, "AdminCog")
    interaction = FakeInteraction(bot)
    await admin.announce.callback(admin, interaction)
    modal = interaction.response.modal
    assert modal is not None

    await admin._preview_announcement(interaction, "Réunion lundi", "Salle 302")
    preview = interaction.response.sent[0]
    assert preview["embed"].title == "📣 Réunion lundi"
    assert bot.announcement is None  # rien n'est enregistré avant la confirmation

    await preview["view"]._on_confirm(interaction)
    assert bot.announcement.title == "Réunion lundi"
    assert bot.announcement.author == "Alice"
    assert bot.admin_log.recent()[0].action == "announce"


async def test_announcement_round_trip_and_corrupted_value(bot):
    bot.save_announcement(Announcement("T", "B", "A", "2026-10-22T13:00:00+00:00"))
    assert bot.announcement == Announcement("T", "B", "A", "2026-10-22T13:00:00+00:00")
    bot.state_repo.set(bot_module.ANNOUNCEMENT_KEY, "{cassé")
    assert bot.announcement is None


async def test_deleted_permanent_message_is_scheduled_for_recreation(bot, monkeypatch):
    calls = []

    async def fake_recreate(key):
        calls.append(key)

    monkeypatch.setattr(bot, "_recreate_later", fake_recreate)
    bot.messages_repo.upsert("bienvenue", 1, 99, "h")
    assert bot.handle_deleted_messages([12345]) == []  # message d'un membre : rien à faire
    assert bot.handle_deleted_messages([99]) == ["bienvenue"]
    assert calls == []  # pas de /setup confirmé : le bot ne republie rien de lui-même
    bot.mark_setup_confirmed()
    bot.handle_deleted_messages([99])
    await asyncio.sleep(0)
    assert calls == ["bienvenue"]


async def test_recreation_can_be_disabled(db, monkeypatch):
    settings = Settings.from_env(
        {"DISCORD_GUILD_ID": str(GUILD_ID), "AUTO_RECREATE_DELETED_MESSAGES": "false"}, require_token=False
    )
    instance = SaeBot(settings, db)
    try:
        instance.mark_setup_confirmed()
        instance.messages_repo.upsert("bienvenue", 1, 99, "h")
        instance.handle_deleted_messages([99])
        assert instance._recreate_tasks == {}
    finally:
        await instance.close()


async def test_error_handler_never_shows_internal_details(bot):
    interaction = FakeInteraction(bot)
    error = app_commands.CommandInvokeError.__new__(app_commands.CommandInvokeError)
    error.original = RuntimeError("chemin secret /srv/bot/data")
    await bot.tree.on_error(interaction, error)
    embed = interaction.response.sent[0]["embed"]
    assert "chemin secret" not in embed_text(embed)
    assert embed.color.value == design.Color.FAILURE


async def test_error_handler_explains_missing_admin_rights(bot):
    interaction = FakeInteraction(bot)
    await bot.tree.on_error(interaction, app_commands.CheckFailure())
    embed = interaction.response.sent[0]["embed"]
    assert "Accès refusé" in embed.title and "Gérer le serveur" in embed.description


# --- Lot 2 : historique, statistiques, classement, tableau de bord, alertes ------------


def choice(value):
    return app_commands.Choice(name=value, value=value)


def add_event(bot, key, kind, **summary):
    from src.database import EventSummary

    bot.gitlab_events_repo.claim(key, kind, summary=EventSummary(dedup_key=key, **summary))


async def test_events_without_data_invents_nothing(bot):
    interaction = FakeInteraction(bot)
    activity = cog(bot, "ActivityCog")
    await activity.events.callback(activity, interaction)
    sent = interaction.response.sent[0]
    assert sent["ephemeral"]
    assert "Aucun événement" in embed_text(sent["embed"])
    assert "aucun événement GitLab enregistré" in sent["embed"].footer.text


async def test_events_filters_by_type_and_text(bot):
    add_event(bot, "p1", "push", action="push", title="Push sur main", ref="main")
    add_event(bot, "pl1", "pipeline", action="failed", title="Pipeline main échoué", ref="main")
    add_event(bot, "pl2", "pipeline", action="success", title="Pipeline feature", ref="feature/x")
    interaction = FakeInteraction(bot)
    activity = cog(bot, "ActivityCog")
    await activity.events.callback(activity, interaction, type=choice("pipeline"), periode=choice("tout"), recherche="main")
    text = embed_text(interaction.response.sent[0]["embed"])
    assert "Pipeline main échoué" in text
    assert "Push sur main" not in text and "Pipeline feature" not in text


async def test_bot_errors_are_reserved_to_admins(bot, monkeypatch):
    bot.bot_errors_repo.record("f", "src.x", "erreur secrète de test")
    activity = cog(bot, "ActivityCog")
    interaction = FakeInteraction(bot)
    await activity.events.callback(activity, interaction, type=choice("errors"))
    text = embed_text(interaction.response.sent[0]["embed"])
    assert "réservées aux administrateurs" in text and "erreur secrète" not in text

    monkeypatch.setattr(activity, "_is_admin", lambda interaction: True)
    interaction = FakeInteraction(bot)
    await activity.events.callback(activity, interaction, type=choice("errors"))
    assert "erreur secrète de test" in embed_text(interaction.response.sent[0]["embed"])


async def test_stats_and_leaderboard_without_enough_data(bot):
    add_event(bot, "p1", "push", action="push", commit_count=4, user_id=1, author="Alice")
    activity = cog(bot, "ActivityCog")
    interaction = FakeInteraction(bot)
    await activity.stats.callback(activity, interaction, periode=choice("30j"))
    stats = embed_text(interaction.response.sent[0]["embed"])
    assert "4 commits" in stats
    assert "écart avec la période précédente" not in stats  # l'historique ne couvre pas la période précédente

    interaction = FakeInteraction(bot)
    await activity.leaderboard.callback(activity, interaction)
    board = embed_text(interaction.response.sent[0]["embed"])
    assert "Pourquoi pas de classement" in board and "Alice" in board
    assert "🥇" not in board


async def test_leaderboard_ranks_with_enough_data(bot):
    for day in range(1, 4):
        add_event(bot, f"a{day}", "push", action="push", user_id=1, author="Alice", occurred_at=f"2026-10-1{day}T10:00:00Z", commit_count=1)
    add_event(bot, "b1", "push", action="push", user_id=2, author="Bob", occurred_at="2026-10-11T10:00:00Z", commit_count=50)
    add_event(bot, "m1", "merge_request", action="merge", subject_id=3, user_id=1, subject_author_id=2, author="Alice",
              occurred_at="2026-10-12T10:00:00Z")
    activity = cog(bot, "ActivityCog")
    interaction = FakeInteraction(bot)
    await activity.leaderboard.callback(activity, interaction, periode=choice("tout"), details=True)
    board = embed_text(interaction.response.sent[0]["embed"])
    assert board.index("Bob") < board.index("Alice")  # la MR fusionnée de Bob passe avant les jours actifs d'Alice
    assert "🥇" in board and "50 commits (indicatif)" in board


async def test_dashboard_refresh_is_debounced(bot, monkeypatch):
    calls = []

    async def fake_refresh():
        calls.append(1)

    monkeypatch.setattr(bot, "refresh_dashboard", fake_refresh)
    assert bot.schedule_dashboard_refresh(delay=0.01)
    assert not bot.schedule_dashboard_refresh(delay=0.01)  # déjà programmée : regroupée
    await asyncio.sleep(0.05)
    assert calls == [1]
    assert bot.schedule_dashboard_refresh(delay=0.01)


async def test_dashboard_is_not_published_before_setup(bot):
    assert await bot.refresh_dashboard() is None


async def test_dashboard_preview_uses_recorded_events(bot):
    add_event(bot, "p1", "push", action="push", commit_count=2)
    payload = bot.render_permanent("tableau-de-bord")
    assert any("2 commits" in f.value for f in payload.fields)


async def test_repeated_errors_are_stored_and_alert_without_channel(bot):
    from src.services.monitoring import ErrorRecord

    record = ErrorRecord(at=None, source="src.x", message="salon 12 introuvable")
    results = [bot.handle_error(record) for _ in range(3)]
    assert results[:2] == [None, None] and results[2] is not None  # 3e occurrence : alerte
    assert bot.bot_errors_repo.recent()[0].count == 3
    assert not bot._alert_tasks  # ALERTS_CHANNEL non défini : rien n'est envoyé


async def test_alert_is_sent_to_configured_channel(db, monkeypatch):
    from src.services.monitoring import ErrorRecord

    settings = Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID), "ALERTS_CHANNEL": "bilan_ia"}, require_token=False)
    instance = SaeBot(settings, db)
    instance.bindings_repo.replace_all({"bilan_ia": 555})
    sent = []

    async def fake_send(client, channel_id, alert):
        sent.append((channel_id, alert.error.message))
        return True

    monkeypatch.setattr(bot_module, "send_alert", fake_send)
    try:
        for _ in range(3):
            instance.handle_error(ErrorRecord(at=None, source="src.x", message="boum"))
        await asyncio.sleep(0)
        assert sent == [(555, "boum")]
    finally:
        await instance.close()


async def test_history_purge_respects_retention(bot):
    add_event(bot, "old", "push", action="push")
    bot.database.conn.execute("UPDATE gitlab_events SET received_at = datetime('now', '-400 days')")
    purged = bot.purge_history()
    assert purged["événement(s) GitLab"] == 1
    assert bot.gitlab_events_repo.recent() == []


# --- Lot 3 : IA -------------------------------------------------------------------------


@pytest.fixture
async def ai_bot(db):
    settings = Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID), "AI_PROVIDER": "fake", "AI_COOLDOWN_SECONDS": "0"},
                                 require_token=False)
    instance = SaeBot(settings, db)
    for extension in EXTENSIONS:
        await instance.load_extension(extension)
    yield instance
    await instance.close()


async def test_ai_commands_explain_when_ai_is_disabled(bot):
    ai = cog(bot, "AICog")
    interaction = FakeInteraction(bot)
    await ai.ask.callback(ai, interaction, question="Quand est la démo ?")
    text = embed_text(interaction.response.sent[0]["embed"])
    assert "IA est désactivée" in text and "AI_PROVIDER=none" in text


async def test_ask_with_fake_provider_cites_sources(ai_bot):
    ai = cog(ai_bot, "AICog")
    interaction = FakeInteraction(ai_bot)
    await ai.ask.callback(ai, interaction, question="Comment configurer les webhooks GitLab ?")
    embed = interaction.followup.sent[0]["embed"]
    text = embed_text(embed)
    assert "Réponse de test" in text and "Sources transmises" in text
    assert "réponse générée par IA (fake-model)" in embed.footer.text
    assert interaction.followup.sent[0]["ephemeral"]


async def test_summary_without_events_does_not_call_the_ai(ai_bot):
    ai = cog(ai_bot, "AICog")
    interaction = FakeInteraction(ai_bot)
    await ai.summary.callback(ai, interaction)
    embed = interaction.followup.sent[0]["embed"]
    assert "rien à résumer" in embed_text(embed) and "aucune IA appelée" in embed.footer.text
    assert ai_bot.ai_provider.calls == []


async def test_review_requires_gitlab_api(ai_bot):
    ai = cog(ai_bot, "AICog")
    interaction = FakeInteraction(ai_bot)
    await ai.review.callback(ai, interaction, mr=12)
    assert "GITLAB_ACCESS_TOKEN" in embed_text(interaction.response.sent[0]["embed"])


async def test_review_with_fake_gitlab_and_fake_ai(ai_bot):
    from src.gitlab.api import FileDiff, MergeRequestInfo

    class FakeAPI:
        async def merge_request(self, iid):
            return MergeRequestInfo(iid, "Score", "", "Bob", "feature/score", "main", "opened", "https://gitlab.com/x/-/merge_requests/12")

        async def merge_request_diffs(self, iid):
            return [FileDiff("src/score.py", "+def score():\n+    return 1\n")]

        async def close(self):
            pass

    ai_bot.gitlab_api = FakeAPI()
    ai_bot.ai_provider.replies = ["Verdict : prête à fusionner"]
    ai = cog(ai_bot, "AICog")
    interaction = FakeInteraction(ai_bot)
    await ai.review.callback(ai, interaction, mr=12)
    embed = interaction.followup.sent[0]["embed"]
    assert "Relecture de !12" in embed.title and "Verdict" in embed.description
    assert "src/score.py" in ai_bot.ai_provider.calls[0][1]


async def test_status_shows_ai_usage(ai_bot):
    general = cog(ai_bot, "GeneralCog")
    interaction = FakeInteraction(ai_bot)
    await general.status.callback(general, interaction)
    text = embed_text(interaction.response.sent[0]["embed"])
    assert "faux fournisseur de test" in text and "0/50 demandes" in text and "/review indisponible" in text


# --- Lot 4 : sauvegardes et fichier de vie ---------------------------------------------------


@pytest.fixture
async def file_bot(tmp_path):
    from src.database import Database

    settings = Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID), "DATABASE_PATH": str(tmp_path / "bot.sqlite3")},
                                 require_token=False)
    database = Database(settings.database_path)
    instance = SaeBot(settings, database)
    for extension in EXTENSIONS:
        await instance.load_extension(extension)
    yield instance
    await instance.close()
    database.close()


async def test_admin_backup_creates_a_verified_copy(file_bot):
    admin = cog(file_bot, "AdminCog")
    interaction = FakeInteraction(file_bot)
    await admin.admin_backup.callback(admin, interaction)
    # Réponse différée d'abord (la copie peut dépasser 3 s), résultat ensuite.
    assert interaction.response.is_done() and not interaction.response.sent
    embed = interaction.followup.sent[0]["embed"]
    assert "Sauvegarde créée" in embed.title and "vérifiée" in embed.description
    assert file_bot.last_backup() is not None
    assert file_bot.admin_log.recent(1)[0].action == "admin backup"


async def test_backup_in_memory_is_refused(bot):
    from src.services.backup import BackupError

    with pytest.raises(BackupError):
        bot.run_backup()


async def test_heartbeat_file(file_bot):
    import json

    file_bot.write_heartbeat()
    data = json.loads(file_bot.settings.heartbeat_path.read_text())
    assert data["ready"] is False and "at" in data


async def test_status_shows_backups(file_bot):
    general = cog(file_bot, "GeneralCog")
    interaction = FakeInteraction(file_bot)
    await general.status.callback(general, interaction)
    assert "aucune sauvegarde" in embed_text(interaction.response.sent[0]["embed"])


async def test_sigterm_closes_the_bot_cleanly(bot):
    bot._on_sigterm()
    for _ in range(20):
        if bot.is_closed():
            break
        await asyncio.sleep(0.01)
    assert bot.is_closed()
