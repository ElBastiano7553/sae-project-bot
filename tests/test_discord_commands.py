"""Vérifie que les commandes slash se chargent correctement, sans se connecter à Discord."""

import pytest
from discord import app_commands

from src.config import Settings
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.services.help import HELP_ENTRIES

ADMIN_COMMANDS = {"admin", "refonte"}
PUBLIC_COMMANDS = {"aide", "etat", "roles", "activite", "projet", "ia", "reunion", "musique"}


@pytest.fixture
async def bot(db):
    settings = Settings.from_env({"DISCORD_GUILD_ID": "42"}, require_token=False)
    instance = SaeBot(settings, db)
    for extension in EXTENSIONS:
        await instance.load_extension(extension)
    yield instance
    await instance.close()


def all_commands(bot):
    """Commandes finales (« admin diagnostic », « reunion ajouter point » pour un sous-groupe)."""
    result = {}

    def visit(command):
        if isinstance(command, app_commands.Group):
            for sub in command.commands:
                visit(sub)
        else:
            result[command.qualified_name] = command

    for command in bot.tree.get_commands():
        visit(command)
    return result


async def test_slash_commands_are_registered(bot):
    names = {command.name for command in bot.tree.get_commands()}
    assert names == ADMIN_COMMANDS | PUBLIC_COMMANDS


async def test_admin_commands_require_manage_guild_by_default(bot):
    commands = {command.name: command for command in bot.tree.get_commands()}
    for name in ADMIN_COMMANDS:
        assert commands[name].default_permissions.manage_guild, f"/{name} doit être masquée par défaut"
    for name, command in all_commands(bot).items():
        if name.split()[0] in ADMIN_COMMANDS:
            assert command.checks, f"/{name} doit avoir une vérification côté bot"
    for name in PUBLIC_COMMANDS:
        assert commands[name].default_permissions is None


# Sous-commandes d'administration dans un groupe ouvert à tous (vérifiées par le bot à chaque appel).
ADMIN_SUBCOMMANDS = {"projet ajouter", "projet modifier", "projet configurer", "projet diagnostic"}


async def test_admin_subcommands_of_public_groups_are_checked_by_the_bot(bot):
    commands = all_commands(bot)
    marked = {name for name, command in commands.items() if command.extras.get("admin")}
    assert marked == ADMIN_SUBCOMMANDS
    for name in marked:
        assert commands[name].checks, f"/{name} doit avoir une vérification côté bot"


async def test_help_lists_exactly_the_registered_commands(bot):
    assert {entry.command for entry in HELP_ENTRIES} == set(all_commands(bot))
    for entry in HELP_ENTRIES:
        expected = entry.command.split()[0] in ADMIN_COMMANDS or entry.command in ADMIN_SUBCOMMANDS
        assert entry.admin == expected, entry.command


async def test_bot_uses_no_privileged_intents(bot):
    assert not bot.intents.message_content
    assert not bot.intents.members
    assert not bot.intents.presences


async def test_members_intent_only_when_welcome_is_enabled(db):
    settings = Settings.from_env({"DISCORD_GUILD_ID": "42", "WELCOME_ON_JOIN": "true"}, require_token=False)
    instance = SaeBot(settings, db)
    try:
        assert instance.intents.members  # nécessaire pour être prévenu des arrivées
        assert not instance.intents.message_content and not instance.intents.presences
    finally:
        await instance.close()


async def test_command_tree_is_compact_and_fits_discord_limits(bot):
    """10 commandes au premier niveau (/musique compris) ; au plus 25 sous-commandes par groupe, descriptions ≤ 100 caractères."""
    top = bot.tree.get_commands()
    assert len(top) <= 10

    def check(command):
        assert 1 <= len(command.description) <= 100, command.qualified_name
        if isinstance(command, app_commands.Group):
            assert len(command.commands) <= 25, command.qualified_name
            for sub in command.commands:
                check(sub)

    for command in top:
        check(command)


async def test_message_service_reads_real_content_files(bot):
    service = bot.build_message_service()
    assert len(service.templates) == 10


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
        for extension in EXTENSIONS:
            await instance.load_extension(extension)
        await instance.start_gitlab_webhooks()
        assert instance.gitlab_server is None
        assert "erreur de test" in instance.gitlab_error
        assert len(instance.build_message_service().templates) == 10  # la V1 fonctionne toujours
    finally:
        await instance.close()


async def test_invalid_gitlab_env_keeps_v1_and_reports_error(db):
    settings = Settings.from_env(
        {"DISCORD_GUILD_ID": "42", "GITLAB_WEBHOOK_SECRET": "court"}, require_token=False
    )
    instance = SaeBot(settings, db)
    try:
        for extension in EXTENSIONS:
            await instance.load_extension(extension)
        await instance.start_gitlab_webhooks()
        assert instance.gitlab_server is None
        assert "trop court" in instance.gitlab_error
        assert len(instance.build_message_service().templates) == 10
    finally:
        await instance.close()


async def test_unexpected_v2_startup_error_keeps_v1(db, unused_tcp_port, monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("disque plein")

    settings = Settings.from_env(
        {"DISCORD_GUILD_ID": "42", "GITLAB_WEBHOOK_SECRET": "secret-de-test-0123456789", "WEBHOOK_PORT": str(unused_tcp_port)},
        require_token=False,
    )
    instance = SaeBot(settings, db)
    # L'oubli des vieux événements a quitté le démarrage de la V2 (voir purge_history) :
    # l'erreur inattendue est provoquée à la création du serveur de webhooks.
    monkeypatch.setattr("src.discord_bot.bot.create_webhook_app", explode)
    try:
        await instance.start_gitlab_webhooks()
        assert "RuntimeError" in instance.gitlab_error
        assert instance.gitlab_server is None or not instance.gitlab_server.listening
    finally:
        await instance.close()


async def test_port_in_use_keeps_v1(db, unused_tcp_port):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", unused_tcp_port))
    blocker.listen()
    settings = Settings.from_env(
        {"DISCORD_GUILD_ID": "42", "GITLAB_WEBHOOK_SECRET": "secret-de-test-0123456789", "WEBHOOK_PORT": str(unused_tcp_port)},
        require_token=False,
    )
    instance = SaeBot(settings, db)
    try:
        for extension in EXTENSIONS:
            await instance.load_extension(extension)
        await instance.start_gitlab_webhooks()
        assert not instance.gitlab_server.listening
        assert "impossible d'écouter" in instance.gitlab_error
        assert len(instance.build_message_service().templates) == 10
    finally:
        await instance.close()
        blocker.close()
