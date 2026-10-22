"""Vérifie que les commandes slash se chargent correctement, sans se connecter à Discord."""

import pytest

from src.config import Settings
from src.discord_bot.bot import SaeBot


@pytest.fixture
async def bot(db):
    settings = Settings.from_env({"DISCORD_GUILD_ID": "42"}, require_token=False)
    instance = SaeBot(settings, db)
    await instance.load_extension("src.discord_bot.cogs.admin")
    yield instance
    await instance.close()


async def test_slash_commands_are_registered(bot):
    names = {command.name for command in bot.tree.get_commands()}
    assert names == {"setup", "refresh", "config", "health"}


async def test_admin_commands_require_manage_guild_by_default(bot):
    commands = {command.name: command for command in bot.tree.get_commands()}
    for name in ("setup", "refresh", "config"):
        assert commands[name].default_permissions.manage_guild
        assert commands[name].checks, f"/{name} doit avoir une vérification côté bot"
    assert commands["health"].default_permissions is None


async def test_bot_uses_no_privileged_intents(bot):
    assert not bot.intents.message_content
    assert not bot.intents.members
    assert not bot.intents.presences


async def test_message_service_reads_real_content_files(bot):
    service = bot.build_message_service()
    assert len(service.templates) == 5
