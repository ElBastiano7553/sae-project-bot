"""Réactivité et fiabilité des interactions : synchronisation des commandes, mesures,
confirmations et boutons en erreur. Faux objets Discord uniquement (aucun réseau) :
cela ne remplace pas le plan de test manuel sur un serveur de test (docs/test-manuel.md).
"""

from __future__ import annotations

import datetime as dt

import discord
import pytest

from src.config import ConfigError, Settings
from src.discord_bot.bot import COMMANDS_SYNC_KEY, EXTENSIONS, SaeBot
from src.discord_bot.meeting_ui import MeetingButton
from src.discord_bot.views import ConfirmView
from src.services.health import HealthStatus, _interactions_check
from src.services.monitoring import SLOW_INTERACTION_MS, InteractionStats


class _Response:
    status = 503
    reason = "Service Unavailable"


@pytest.fixture
async def bot(db):
    instance = SaeBot(Settings.from_env({"DISCORD_GUILD_ID": "42"}, require_token=False), db)
    for extension in EXTENSIONS:
        await instance.load_extension(extension)
    yield instance
    await instance.close()


# --- Synchronisation des commandes slash -----------------------------------------------------------


async def test_commands_are_synced_only_when_they_change(bot, monkeypatch):
    calls = []

    async def fake_sync(*, guild=None):
        calls.append(guild.id)
        return [object()] * 3

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    assert await bot.sync_commands() == 3
    assert await bot.sync_commands() == 0  # redémarrage sans changement : aucun appel à Discord
    assert len(calls) == 1

    bot.tree.get_command("status").description = "Nouvelle description"
    assert await bot.sync_commands() == 3  # une description modifiée suffit à resynchroniser
    assert await bot.sync_commands(force=True) == 3
    assert len(calls) == 3


async def test_failed_sync_keeps_the_bot_running_and_retries_next_start(bot, monkeypatch, caplog):
    async def failing_sync(*, guild=None):
        raise discord.HTTPException(_Response(), "indisponible")

    monkeypatch.setattr(bot.tree, "sync", failing_sync)
    assert await bot.sync_commands() is None
    assert bot.state_repo.get(COMMANDS_SYNC_KEY) is None  # rien n'est marqué comme synchronisé
    assert "restent utilisables" in caplog.text


# --- Mesure de la réactivité -------------------------------------------------------------------------


def test_interaction_stats_summary():
    stats = InteractionStats()
    assert stats.summary().total == 0
    for ms in (120, 180, 250):
        stats.record("help", ms)
    assert stats.record("refonte", SLOW_INTERACTION_MS + 1000) is True
    stats.record("status", 90, ok=False)
    summary = stats.summary()
    assert (summary.total, summary.slow, summary.errors) == (5, 1, 1)
    assert summary.median_ms == 180 and summary.slowest.startswith("/refonte") and summary.failing == "status"


def test_interactions_health_line_warns_only_when_frequent():
    stats = InteractionStats()
    for _ in range(19):
        stats.record("help", 150)
    stats.record("ask", 4000)
    assert _interactions_check(stats.summary()).status is HealthStatus.OK  # une lenteur isolée sur 20
    for _ in range(3):
        stats.record("ask", 4000)
    check = _interactions_check(stats.summary())
    assert check.status is HealthStatus.WARNING and "lente(s)" in check.detail


class FakeCommand:
    qualified_name = "reunion creer"


class TimedInteraction:
    def __init__(self, client, seconds_ago: float) -> None:
        self.client = client
        self.command = FakeCommand()
        self.created_at = discord.utils.utcnow() - dt.timedelta(seconds=seconds_ago)


async def test_slow_command_is_logged_without_content(bot, caplog):
    bot.record_interaction(TimedInteraction(bot, 0.1), ok=True)
    assert "lente" not in caplog.text
    bot.record_interaction(TimedInteraction(bot, 4), ok=True)
    assert "Commande /reunion creer lente" in caplog.text
    assert bot.interaction_stats.summary().slow == 1


async def test_status_shows_reactivity(bot):
    from src.discord_bot.cogs.general import build_status_embed

    bot.record_interaction(TimedInteraction(bot, 0.2), ok=True)
    text = " ".join(f.name + " " + f.value for f in build_status_embed(bot).fields)
    assert "Réactivité" in text and "1 commande(s) depuis le démarrage" in text


# --- Confirmations et boutons : jamais de résultat ambigu -------------------------------------------------


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.edits: list[dict] = []
        self.sent: list[tuple] = []
        self.deferred = 0

    def is_done(self) -> bool:
        return self.done

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        self.done = True

    async def defer(self, **kwargs):
        self.deferred += 1
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeInteraction:
    def __init__(self, user_id: int = 7, client=None) -> None:
        self.user = FakeUser(user_id)
        self.client = client
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.original_edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


async def test_confirm_failure_is_never_shown_as_success(caplog):
    async def explode(interaction):
        raise RuntimeError("panne")

    view = ConfirmView(7, explode, command="/setup")
    interaction = FakeInteraction()
    await view.confirm.callback(interaction)
    assert interaction.response.edits[0]["content"] == "Application en cours…"
    final = interaction.original_edits[-1]["content"]
    assert final.startswith("**Échec**") and "/status" in final and "panne" not in final
    assert "Erreur pendant la confirmation de /setup" in caplog.text


async def test_confirm_configuration_error_is_explained():
    async def misconfigured(interaction):
        raise ConfigError("DISCORD_GUILD_ID ne correspond pas à ce serveur.")

    view = ConfirmView(7, misconfigured)
    interaction = FakeInteraction()
    await view.confirm.callback(interaction)
    assert "Problème de configuration" in interaction.original_edits[-1]["content"]


async def test_confirm_runs_once_and_answers_a_second_click():
    runs = []

    async def action(interaction):
        runs.append(1)

    view = ConfirmView(7, action)
    first = FakeInteraction()
    assert await view.interaction_check(first) is True
    await view.confirm.callback(first)
    second = FakeInteraction()
    assert await view.interaction_check(second) is False  # aucun « Échec de l'interaction » muet
    assert second.response.deferred == 1 and runs == [1]


async def test_confirm_is_reserved_to_its_author():
    view = ConfirmView(7, lambda i: None)
    other = FakeInteraction(user_id=8)
    assert await view.interaction_check(other) is False
    assert "Seule la personne" in other.response.sent[0][0]


class ExplodingCog:
    async def handle_button(self, interaction, meeting_id, action, revision):
        raise RuntimeError("panne")


class FakeClient:
    def __init__(self, cog) -> None:
        self._cog = cog

    def get_cog(self, name):
        return self._cog


async def test_meeting_button_error_gets_a_clear_answer(caplog):
    interaction = FakeInteraction(client=FakeClient(ExplodingCog()))
    await MeetingButton(3, "join", 1).callback(interaction)
    content, kwargs = interaction.response.sent[0]
    assert "n'a pas abouti" in content and kwargs["ephemeral"] is True
    assert "bouton join de la réunion 3" in caplog.text


async def test_meeting_button_error_after_response_uses_followup():
    class LateFailure:
        async def handle_button(self, interaction, *args):
            await interaction.response.edit_message(content="ok")
            raise RuntimeError("panne")

    interaction = FakeInteraction(client=FakeClient(LateFailure()))
    await MeetingButton(3, "next", 1).callback(interaction)
    assert interaction.followup.sent and "n'a pas abouti" in interaction.followup.sent[0][0]
