"""Commande /refonte de bout en bout, sur un faux serveur Discord en mémoire (aucun réseau, aucun vrai serveur).

Ces tests simulent Discord : ils ne remplacent pas un essai sur un serveur de test.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest

from src.config import Settings
from src.database import Database
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.discord_bot.redesign_ui import ANALYSIS, CLOSED, CONFIRM, DELETE_CONFIRM, DELETIONS, DONE, PREVIEW, RETOUR
from src.services.meetings import Actor as MeetingActor
from src.services.server_redesign import OpKind, ResultStatus
from tests.fake_guild import GUILD_ID, sae_server

ADMIN_ID, MEMBER_ID = 111111111111111111, 222222222222222222


class FakeMember(discord.Member):
    """Vrai type Member (isinstance), avec des permissions et un rôle le plus haut choisis par le test."""

    def __init__(self, user_id: int, top: int = 50, **perms) -> None:
        self._user = SimpleNamespace(id=user_id, name=f"user{user_id}", display_name=f"user{user_id}", bot=False)
        self._fake_perms = discord.Permissions(**perms)
        self._top = SimpleNamespace(position=top)

    @property
    def guild_permissions(self):
        return self._fake_perms

    @property
    def top_role(self):
        return self._top

    @property
    def roles(self):
        return []

    @property
    def display_name(self):
        return self._user.display_name

    def __str__(self):
        return self._user.name


class FakeResponse:
    def __init__(self) -> None:
        self.sent, self.edited, self._done = [], [], False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self._done = True

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)
        self._done = True

    async def defer(self, **kwargs):
        self._done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeInteraction:
    @property
    def guild_id(self):  # comme discord.Interaction : identifiant du serveur, None en message privé
        return getattr(getattr(self, "guild", None), "id", None)

    def __init__(self, client, guild, user) -> None:
        self.client, self.guild, self.user = client, guild, user
        self.response, self.followup = FakeResponse(), FakeFollowup()
        self.edits: list[dict] = []
        self.command = None
        self.data = {}

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)

    def last_embed(self):
        for payload in reversed(self.response.edited + self.edits):
            if payload.get("embed") is not None:
                return payload["embed"]
        return None


def text(embed) -> str:
    return " ".join([embed.title or "", embed.description or "", *[f"{f.name} {f.value}" for f in embed.fields]])


ADMIN = FakeMember(ADMIN_ID, manage_guild=True, manage_channels=True, manage_roles=True)



async def make_env(db, monkeypatch, tmp_path, guild):
    bot = SaeBot(Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID)}, require_token=False), db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    monkeypatch.setattr(SaeBot, "target_guild", property(lambda self: guild))
    cog = bot.get_cog("RedesignCog")
    cog.settle_delay = 0
    cog.journal_dir = tmp_path / "refonte"
    return bot, cog


@pytest.fixture
async def env(db, monkeypatch, tmp_path):
    guild = sae_server()
    guild.add_channel("memes")  # salon personnel hors cible : jamais touché
    bot, cog = await make_env(db, monkeypatch, tmp_path, guild)
    yield bot, cog, guild
    await bot.close()


async def open_refonte(bot, cog, guild, user=ADMIN, **options):
    interaction = FakeInteraction(bot, guild, user)
    await cog.lancer.callback(cog, interaction, **options)
    return interaction


def view_of(interaction):
    return interaction.followup.sent[0]["view"]


async def click(bot, cog, guild, view, action, user=ADMIN):
    interaction = FakeInteraction(bot, guild, user)
    if not await view.interaction_check(interaction):
        return interaction
    await cog.handle_redesign_button(interaction, view, action)
    return interaction


async def apply_refonte(bot, cog, guild, user=ADMIN, **options):
    view = view_of(await open_refonte(bot, cog, guild, user=user, **options))
    await click(bot, cog, guild, view, "apply", user=user)
    assert view.session.mode == CONFIRM
    interaction = await click(bot, cog, guild, view, "confirm", user=user)
    return view, interaction


def labels(view) -> list[str]:
    return [getattr(item, "label", None) or getattr(item, "placeholder", "") for item in view.children]


# --- Analyse et aperçu ----------------------------------------------------------------------


async def test_analysis_and_preview_have_no_side_effect(env):
    bot, cog, guild = env
    before = guild.tree()
    interaction = await open_refonte(bot, cog, guild)
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"]
    view = view_of(interaction)
    assert view.session.mode == ANALYSIS
    assert {"Analyser le serveur", "Prévisualiser", "Appliquer les changements", "Annuler"} <= set(labels(view))
    assert {"Profil du serveur", "Style d'affichage"} <= set(labels(view))
    await click(bot, cog, guild, view, "preview")
    assert view.session.mode == PREVIEW and len(view.pages()) >= 2
    await click(bot, cog, guild, view, "next")
    await click(bot, cog, guild, view, "analyse")
    assert guild.calls == [] and guild.tree() == before
    assert bot.bindings_repo.get_all() == {} and bot.admin_log.recent() == []


async def test_preview_shows_everything_the_user_must_know(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild, suppressions=True))
    await click(bot, cog, guild, view, "preview")
    pages = view.pages()
    for page in pages:
        assert len(page) <= 6000 and len(page.description or "") <= 4096
    whole = " ".join(text(p) for p in pages)
    for expected in ("Structure cible", "Rôles à créer", "#C9A227", "Catégories à renommer", "Salons à déplacer",
                     "Permissions à corriger", "memes", "Risques", "Suppressions"):
        assert expected in whole, expected
    assert "Examiner les suppressions" in labels(view)


async def test_profile_and_style_menus_recompute_the_plan(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    interaction = FakeInteraction(bot, guild, ADMIN)
    await cog.handle_redesign_choice(interaction, view, "style", "sobre")
    assert view.session.options.style == "sobre"
    assert "ACCUEIL" in {op.target for op in view.session.plan.of(OpKind.EDIT_CATEGORY)}
    await cog.handle_redesign_choice(interaction, view, "profile", "communautaire")
    assert view.session.options.style == "communautaire"
    assert "Modérateur" in {op.target for op in view.session.plan.of(OpKind.CREATE_ROLE)}
    assert guild.calls == []


async def test_unknown_profile_is_explained(env):
    bot, cog, guild = env
    interaction = await open_refonte(bot, cog, guild, profil="inexistant")
    assert "profil inconnu" in interaction.followup.sent[0]["embed"].description


# --- Contrôles d'accès ------------------------------------------------------------------------


async def test_member_without_admin_rights_is_refused(env):
    bot, cog, guild = env
    interaction = await open_refonte(bot, cog, guild, user=FakeMember(MEMBER_ID))
    assert "réservée aux administrateurs" in interaction.response.sent[0]["embed"].description
    assert interaction.followup.sent == []


async def test_admin_without_manage_channels_is_refused(env):
    bot, cog, guild = env
    interaction = await open_refonte(bot, cog, guild, user=FakeMember(ADMIN_ID, manage_guild=True))
    assert "Gérer les salons" in interaction.response.sent[0]["embed"].description


async def test_another_user_cannot_click_the_buttons(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    other = await click(bot, cog, guild, view, "confirm", user=FakeMember(MEMBER_ID, administrator=True))
    assert "Seule la personne" in other.response.sent[0]["content"]
    assert guild.calls == []


async def test_rights_are_checked_again_at_confirmation(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    await click(bot, cog, guild, view, "apply")
    demoted = FakeMember(ADMIN_ID, manage_guild=True)  # même personne, « Gérer les salons » retiré entre-temps
    interaction = await click(bot, cog, guild, view, "confirm", user=demoted)
    assert "Gérer les salons" in interaction.response.sent[0]["embed"].description
    assert guild.calls == []


async def test_missing_bot_permission_blocks_the_apply_button_and_the_server(env):
    bot, cog, guild = env
    guild.bot_permissions = {"view_channel"}
    view = view_of(await open_refonte(bot, cog, guild))
    apply = next(item for item in view.children if getattr(item, "label", "") == "Appliquer les changements")
    assert apply.disabled
    interaction = await click(bot, cog, guild, view, "apply")  # clic forcé : refusé côté serveur
    assert "ne peut pas être appliqué" in interaction.response.sent[0]["embed"].description
    view.session.mode = CONFIRM
    await click(bot, cog, guild, view, "confirm")
    assert guild.calls == []


async def test_user_without_manage_roles_gets_no_role_changes(env):
    bot, cog, guild = env
    member = FakeMember(ADMIN_ID, manage_guild=True, manage_channels=True)
    view, _ = await apply_refonte(bot, cog, guild, user=member)
    assert not [c for c in guild.calls if c[0] in ("create_role", "edit_role", "role_positions")]
    assert view.session.mode == DONE


# --- Application ---------------------------------------------------------------------------------


async def test_confirmed_refonte_transforms_the_server_and_reports_faithfully(env):
    bot, cog, guild = env
    memes = guild.by_name("memes")
    view, interaction = await apply_refonte(bot, cog, guild)
    assert view.session.mode == DONE
    tree = guild.tree()
    assert tree["💬 ESPACE COMMUN"] == ["💬・général"]
    assert tree["📋 ORGANISATION"] == ["🗓️・réunions", "📝・comptes-rendus", "📆・planning"]
    assert "🦊・activité-git" in tree["💻 DÉVELOPPEMENT"]
    assert tree["🛡️ ADMINISTRATION"] == ["🧾・bot-logs"]
    assert list(tree)[:2] == ["🏠 ACCUEIL", "💬 ESPACE COMMUN"]
    assert guild.role("Responsable projet").colour.value == 0xC9A227
    assert (memes.name, memes.category_id) == ("memes", None) and memes.overwrites == {}
    assert guild.by_name("04 · GITLAB AUTOMATISÉ") is not None  # vidée, jamais supprimée sans confirmation séparée
    outcome = view.session.outcome
    assert not outcome.with_status(ResultStatus.FAILED) and outcome.verified and outcome.remaining == ()
    assert interaction.last_embed().title.startswith("Refonte")
    assert bot.admin_log.recent()[0].action == "refonte"


async def test_staff_category_hides_from_everyone_but_never_from_the_bot(env):
    bot, cog, guild = env
    await apply_refonte(bot, cog, guild)
    admin_cat = guild.by_name("🛡️ ADMINISTRATION")
    assert admin_cat.overwrites[guild.default_role].view_channel is False
    assert admin_cat.overwrites[guild.me].view_channel is True
    logs = guild.by_name("🧾・bot-logs")
    assert logs.permissions_for(guild.me).view_channel


async def test_configuration_is_saved_by_identifier(env):
    bot, cog, guild = env
    await apply_refonte(bot, cog, guild)
    bindings = bot.bindings_repo.get_all()
    assert bindings["comptes_rendus"] == guild.by_name("📝・comptes-rendus").id
    assert bindings["gitlab_activite"] == guild.by_name("🦊・activité-git").id
    assert set(bot.category_bindings) == {"accueil", "commun", "conception", "organisation", "developpement", "livrables",
                                          "vocal", "administration"}
    assert bot.role_bindings["testeur"] == guild.role("Testeur").id


async def test_second_run_is_idempotent(env):
    bot, cog, guild = env
    await apply_refonte(bot, cog, guild)
    tree = guild.tree()
    guild.calls.clear()
    view = view_of(await open_refonte(bot, cog, guild))
    assert [op for op in view.session.plan.actions if op.kind is not OpKind.BIND] == []
    await click(bot, cog, guild, view, "apply")
    if view.session.mode == CONFIRM:
        await click(bot, cog, guild, view, "confirm")
    assert guild.tree() == tree and guild.calls == []


async def test_changes_since_preview_invalidate_the_plan(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    await click(bot, cog, guild, view, "apply")
    fingerprint = view.session.plan.fingerprint
    guild.by_name("bilan-ia").name = "bilan-ia-v2"  # changement concurrent
    interaction = await click(bot, cog, guild, view, "confirm")
    assert view.session.mode == PREVIEW and view.session.plan.fingerprint != fingerprint
    assert "Le serveur a changé" in text(interaction.last_embed())
    assert guild.calls == [] and bot.bindings_repo.get_all() == {}


async def test_double_click_never_applies_twice(env):
    bot, cog, guild = env
    view, _ = await apply_refonte(bot, cog, guild)
    calls = len(guild.calls)
    view.session.mode = CONFIRM  # bouton resté affiché chez un client lent
    await click(bot, cog, guild, view, "confirm")
    assert len(guild.calls) == calls


async def test_partial_failure_is_reported_and_other_operations_continue(env):
    bot, cog, guild = env
    response = SimpleNamespace(status=403, reason="Forbidden")
    guild.fail("edit", "bugs-et-tests", discord.Forbidden(response, "Missing Permissions"))
    view, interaction = await apply_refonte(bot, cog, guild)
    outcome = view.session.outcome
    failed = outcome.with_status(ResultStatus.FAILED)
    assert [r.operation.key for r in failed] == ["bugs"]
    assert "permission" in failed[0].detail.lower()
    assert "partiel" in interaction.last_embed().title.lower()
    assert guild.by_name("💬・général") is not None  # les autres opérations ont été faites
    assert outcome.remaining  # la vérification finale signale ce qui reste
    assert cog.load_journal()["status"] == "partielle"


async def test_interrupted_application_leaves_a_usable_journal(env):
    bot, cog, guild = env
    guild.fail("create_channel", "🧾・bot-logs", asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await apply_refonte(bot, cog, guild)
    journal = cog.load_journal()
    assert journal["status"] == "interrompue" and journal["results"]
    assert not bot.redesign_lock.locked()
    # Relancer reprend ce qui manque, sans rien refaire.
    plan = cog.analyse(guild, view_of(await open_refonte(bot, cog, guild)).session.options, ADMIN)[0]
    assert {op.target for op in plan.of(OpKind.CREATE_CHANNEL)} == {"🧾・bot-logs"}


async def test_cancel_changes_nothing(env):
    bot, cog, guild = env
    view = view_of(await open_refonte(bot, cog, guild))
    interaction = await click(bot, cog, guild, view, "cancel")
    assert view.session.mode == CLOSED and "rien n'a été modifié" in interaction.response.edited[0]["content"]
    assert guild.calls == []


async def test_expired_view_says_what_was_changed(env):
    bot, cog, guild = env
    opening = await open_refonte(bot, cog, guild)
    view = view_of(opening)
    await view.on_timeout()
    assert "rien n'a été modifié" in opening.edits[-1]["content"]
    view.session.applied = True
    await view.on_timeout()
    assert "déjà appliqués restent" in opening.edits[-1]["content"]


# --- Suppressions : liste séparée, confirmation distincte -----------------------------------------


async def test_deletions_need_their_own_confirmation(env):
    bot, cog, guild = env
    view, _ = await apply_refonte(bot, cog, guild, suppressions=True)
    assert guild.by_name("04 · GITLAB AUTOMATISÉ") is not None
    assert "Examiner les suppressions" in labels(view)
    await click(bot, cog, guild, view, "deletions")
    assert view.session.mode == DELETIONS and "04 · GITLAB AUTOMATISÉ" in text(view.current_embed())
    await click(bot, cog, guild, view, "delete_review")
    assert view.session.mode == DELETE_CONFIRM and "définitiv" in text(view.current_embed()).lower()
    await click(bot, cog, guild, view, "delete_confirm")
    assert guild.by_name("04 · GITLAB AUTOMATISÉ") is None
    assert guild.by_name("memes") is not None  # jamais proposé : fonction inconnue, contient des messages
    assert bot.admin_log.recent()[0].action == "refonte suppressions"


async def test_unconfirmed_deletions_are_never_applied(env):
    bot, cog, guild = env
    view, _ = await apply_refonte(bot, cog, guild, suppressions=True)
    await click(bot, cog, guild, view, "deletions")
    await click(bot, cog, guild, view, "cancel")
    assert guild.by_name("04 · GITLAB AUTOMATISÉ") is not None
    assert not [c for c in guild.calls if c[0] in ("delete", "delete_role")]


async def test_deletion_list_changed_since_review_is_not_applied(env):
    bot, cog, guild = env
    view, _ = await apply_refonte(bot, cog, guild, suppressions=True)
    await click(bot, cog, guild, view, "deletions")
    await click(bot, cog, guild, view, "delete_review")
    guild.add_channel("nouveau", guild.by_name("04 · GITLAB AUTOMATISÉ"))
    interaction = await click(bot, cog, guild, view, "delete_confirm")
    assert "a changé" in text(interaction.last_embed())
    assert guild.by_name("04 · GITLAB AUTOMATISÉ") is not None


async def test_without_the_option_deletions_cannot_be_reviewed(env):
    bot, cog, guild = env
    view, _ = await apply_refonte(bot, cog, guild)
    assert "Examiner les suppressions" not in labels(view)


# --- Retour arrière --------------------------------------------------------------------------------


async def test_rollback_from_the_journal(env):
    bot, cog, guild = env
    general = guild.by_name("discussion-generale")
    old_category = general.category.name
    await apply_refonte(bot, cog, guild)
    assert general.name == "💬・général"

    interaction = FakeInteraction(bot, guild, ADMIN)
    await cog.retour.callback(cog, interaction)
    view = view_of(interaction)
    assert view.session.kind == RETOUR
    whole = text(view.current_embed())
    assert "Journal" in whole
    await click(bot, cog, guild, view, "apply")
    await click(bot, cog, guild, view, "confirm")
    assert view.session.mode == DONE
    assert (general.name, general.category.name) == ("discussion-generale", old_category)
    assert bot.bindings_repo.get_all() == {}  # associations d'avant la refonte
    # Éléments créés : suppression proposée séparément, avec ses limites affichées.
    assert view.session.plan.can_delete
    await click(bot, cog, guild, view, "deletions")
    await click(bot, cog, guild, view, "delete_review")
    await click(bot, cog, guild, view, "delete_confirm")
    assert guild.by_name("📋 ORGANISATION") is None and guild.role("Testeur") is None
    assert guild.by_name("memes") is not None


async def test_rollback_keeps_what_changed_since_and_says_so(env):
    bot, cog, guild = env
    await apply_refonte(bot, cog, guild)
    guild.by_name("🐛・bugs-et-problèmes").name = "bugs"
    guild.by_name("🗓️・réunions").last_message_id = 77
    interaction = FakeInteraction(bot, guild, ADMIN)
    await cog.retour.callback(cog, interaction)
    view = view_of(interaction)
    await click(bot, cog, guild, view, "preview")
    whole = " ".join(text(p) for p in view.pages())
    assert "renommé depuis la refonte" in whole and "contient des messages" in whole
    assert "ne reviennent jamais" in whole


async def test_rollback_without_journal_is_explained(env):
    bot, cog, guild = env
    interaction = FakeInteraction(bot, guild, ADMIN)
    await cog.retour.callback(cog, interaction)
    assert "Aucun journal" in interaction.response.sent[0]["embed"].description


# --- Données préservées : réunions, sauvegarde, journal -----------------------------------------------


async def test_meetings_survive_and_reports_go_to_the_new_minutes_channel(env):
    bot, cog, guild = env
    planning = guild.by_name("planning-et-deadlines")
    leader = MeetingActor(ADMIN_ID, "Alice")
    meeting = bot.meetings.create(leader, title="Point sprint", steps=["Bilan"], channel_id=planning.id).meeting
    bot.meetings.set_panel(meeting.id, planning.id, 123456)
    before = bot.meetings.get(meeting.id)

    await apply_refonte(bot, cog, guild)

    after = bot.meetings.get(meeting.id)
    assert (after.meeting.channel_id, after.meeting.panel_message_id) == (planning.id, 123456)
    assert after.meeting.revision == before.meeting.revision and len(bot.meetings.repo.list()) == 1
    assert planning.name == "📆・planning"  # renommé : même identifiant, panneau toujours valide
    from src.discord_bot.cogs.meetings import REPORT_CHANNEL_KEYS

    assert bot.bindings_repo.get_all()[REPORT_CHANNEL_KEYS[0]] == guild.by_name("📝・comptes-rendus").id


async def test_backup_and_journal_are_written_before_and_after(tmp_path, monkeypatch):
    database = Database(tmp_path / "bot.sqlite3")
    settings = Settings.from_env(
        {"DISCORD_GUILD_ID": str(GUILD_ID), "DATABASE_PATH": str(tmp_path / "bot.sqlite3"), "BACKUP_INTERVAL_HOURS": "0"},
        require_token=False,
    )
    bot = SaeBot(settings, database)
    try:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        guild = sae_server()
        monkeypatch.setattr(SaeBot, "target_guild", property(lambda self: guild))
        cog = bot.get_cog("RedesignCog")
        cog.settle_delay = 0
        assert cog.journal_dir == tmp_path / "refonte"
        view, _ = await apply_refonte(bot, cog, guild)
        outcome = view.session.outcome
        assert outcome.backup and (tmp_path / "backups" / outcome.backup).exists()
        journal = json.loads((tmp_path / "refonte" / outcome.journal).read_text(encoding="utf-8"))
        assert journal["status"] == "terminee"
        snapshot = journal["before"]["snapshot"]
        assert any(ch["name"] == "decisions-et-comptes-rendus" for ch in snapshot["channels"])  # état d'avant complet
        assert all("overwrites" in ch and "position" in ch for ch in snapshot["channels"])
        assert any(r["undo"] for r in journal["results"])
        assert "token" not in json.dumps(journal).lower()
        assert cog.journal_names() == [outcome.journal]
    finally:
        await bot.close()
        database.close()


async def test_refused_backup_means_nothing_is_applied(env, monkeypatch):
    bot, cog, guild = env
    from src.services.backup import BackupError

    monkeypatch.setattr(type(bot.database), "in_memory", False, raising=False)
    bot.database.in_memory = False

    def broken(reason=""):
        raise BackupError("disque plein")

    monkeypatch.setattr(bot, "run_backup", broken)
    view, _ = await apply_refonte(bot, cog, guild)
    assert all(r.status is ResultStatus.SKIPPED for r in view.session.outcome.results)
    assert guild.calls == [] and bot.bindings_repo.get_all() == {}
    assert "sauvegarde impossible" in bot.admin_log.recent()[0].outcome


# --- Serveur Communauté ----------------------------------------------------------------------------


async def test_community_server_gets_a_real_announcement_channel(db, monkeypatch, tmp_path):
    guild = sae_server(community=True)
    bot, cog = await make_env(db, monkeypatch, tmp_path, guild)
    try:
        await apply_refonte(bot, cog, guild)
        assert guild.by_name("📢・annonces").type == discord.ChannelType.news
    finally:
        await bot.close()
