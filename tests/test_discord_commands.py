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


async def test_v2_is_disabled_without_webhook_secret(bot):
    await bot.start_gitlab_webhooks()
    assert bot.gitlab_server is None
    assert bot.gitlab_error is None


async def test_v2_starts_and_stops_with_the_bot(db, unused_tcp_port):
    settings = Settings.from_env(
        {
            "DISCORD_GUILD_ID": "42",
            "GITLAB_WEBHOOK_SECRET": "secret-de-test-0123456789",
            "WEBHOOK_PORT": str(unused_tcp_port),
        },
        require_token=False,
    )
    instance = SaeBot(settings, db)
    try:
        await instance.start_gitlab_webhooks()
        assert instance.gitlab_server is not None and instance.gitlab_server.listening
        assert instance.gitlab_error is None
    finally:
        await instance.close()
    assert not instance.gitlab_server.listening


async def test_v2_failure_does_not_break_v1(db, unused_tcp_port, monkeypatch):
    from src.config import ConfigError

    def broken_rules(**kwargs):
        raise ConfigError("gitlab.toml : erreur de test")

    monkeypatch.setattr("src.discord_bot.bot.load_gitlab_rules", broken_rules)
    settings = Settings.from_env(
        {"DISCORD_GUILD_ID": "42", "GITLAB_WEBHOOK_SECRET": "secret-de-test-0123456789", "WEBHOOK_PORT": str(unused_tcp_port)},
        require_token=False,
    )
    instance = SaeBot(settings, db)
    try:
        await instance.load_extension("src.discord_bot.cogs.admin")
        await instance.start_gitlab_webhooks()
        assert instance.gitlab_server is None
        assert "erreur de test" in instance.gitlab_error
        assert len(instance.build_message_service().templates) == 5  # la V1 fonctionne toujours
    finally:
        await instance.close()
