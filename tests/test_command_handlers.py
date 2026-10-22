"""Exécution des commandes avec une fausse interaction Discord (aucune connexion réseau)."""

from __future__ import annotations

import asyncio

import discord
import pytest
from discord import app_commands

from src.config import Settings
from src.discord_bot import bot as bot_module
from src.discord_bot.bot import EXTENSIONS, SaeBot
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
    me = None

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
    assert "SQLite répond (schéma v4)" in text
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
    assert embed.color.value == 0xE74C3C


async def test_error_handler_explains_missing_admin_rights(bot):
    interaction = FakeInteraction(bot)
    await bot.tree.on_error(interaction, app_commands.CheckFailure())
    embed = interaction.response.sent[0]["embed"]
    assert "Accès refusé" in embed.title and "Gérer le serveur" in embed.description
