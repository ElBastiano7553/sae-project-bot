"""Configuration GitHub, démarrage/arrêt avec le bot et non-régression GitLab."""

import asyncio
import socket

import pytest

from src.config import DEFAULT_GITHUB_REPOSITORY, Settings, secret_values
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.services.health import GitHubSummary, HealthStatus, build_health_checks

GITHUB_SECRET = "secret-github-de-test-0123456789"
GITLAB_SECRET = "secret-de-test-0123456789"


def settings(**env):
    return Settings.from_env({"DISCORD_GUILD_ID": "42", **env}, require_token=False)


# --- Configuration ------------------------------------------------------------------


def test_github_is_disabled_by_default():
    config = settings()
    assert not config.github_webhooks_enabled
    assert not config.github_requested
    assert config.github_config_error is None
    assert config.github_repository == DEFAULT_GITHUB_REPOSITORY == "ElBastiano7553/game-project"
    assert (config.github_webhook_host, config.github_webhook_port) == ("127.0.0.1", 8081)


def test_github_settings_are_read():
    config = settings(
        GITHUB_WEBHOOK_SECRET=GITHUB_SECRET,
        GITHUB_WEBHOOK_REPOSITORY="Autre/depot.jeu",
        GITHUB_WEBHOOK_HOST="0.0.0.0",
        GITHUB_WEBHOOK_PORT="9000",
    )
    assert config.github_webhooks_enabled
    assert (config.github_repository, config.github_webhook_host, config.github_webhook_port) == ("Autre/depot.jeu", "0.0.0.0", 9000)


def test_github_secret_is_hidden_from_repr_and_masked_in_logs():
    config = settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    assert GITHUB_SECRET not in repr(config)
    assert GITHUB_SECRET in secret_values({"GITHUB_WEBHOOK_SECRET": GITHUB_SECRET})


@pytest.mark.parametrize(
    "env, message",
    [
        ({"GITHUB_WEBHOOK_SECRET": "court"}, "trop court"),
        ({"GITHUB_WEBHOOK_SECRET": GITHUB_SECRET, "GITHUB_WEBHOOK_PORT": "abc"}, "GITHUB_WEBHOOK_PORT"),
        ({"GITHUB_WEBHOOK_SECRET": GITHUB_SECRET, "GITHUB_WEBHOOK_PORT": "70000"}, "GITHUB_WEBHOOK_PORT"),
        ({"GITHUB_WEBHOOK_SECRET": GITHUB_SECRET, "GITHUB_WEBHOOK_REPOSITORY": "pas-de-slash"}, "propriétaire/dépôt"),
        ({"GITHUB_WEBHOOK_SECRET": GITHUB_SECRET, "GITHUB_WEBHOOK_REPOSITORY": "a/b/c"}, "propriétaire/dépôt"),
        (
            {"GITHUB_WEBHOOK_SECRET": GITHUB_SECRET, "GITLAB_WEBHOOK_SECRET": GITLAB_SECRET, "GITHUB_WEBHOOK_PORT": "8080"},
            "déjà utilisé",
        ),
    ],
)
def test_invalid_github_settings_disable_only_github(env, message):
    config = settings(**env)
    assert not config.github_webhooks_enabled
    assert config.github_requested
    assert message in config.github_config_error
    assert GITHUB_SECRET not in config.github_config_error


def test_invalid_github_settings_are_ignored_without_secret():
    config = settings(GITHUB_WEBHOOK_PORT="abc", GITHUB_WEBHOOK_REPOSITORY="??")
    assert config.github_config_error is None


def test_github_does_not_change_gitlab_settings():
    without = settings(GITLAB_WEBHOOK_SECRET=GITLAB_SECRET, GITLAB_PROJECT_ID="12")
    with_github = settings(GITLAB_WEBHOOK_SECRET=GITLAB_SECRET, GITLAB_PROJECT_ID="12", GITHUB_WEBHOOK_SECRET="court")
    for name in ("gitlab_webhook_secret", "gitlab_project_id", "webhook_host", "webhook_port", "gitlab_config_error"):
        assert getattr(without, name) == getattr(with_github, name)
    assert with_github.gitlab_webhooks_enabled


def test_github_actions_variable_github_repository_is_not_used():
    # GitHub Actions définit GITHUB_REPOSITORY (dépôt du bot) : il ne doit pas changer le dépôt autorisé.
    config = settings(GITHUB_REPOSITORY="ElBastiano7553/sae-project-bot", GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    assert config.github_repository == "ElBastiano7553/game-project"


# --- /etat -------------------------------------------------------------------------


def checks(**github):
    return {
        check.name: check
        for check in build_health_checks(
            latency_ms=50, database_ok=True, guild_found=True, structure=None, setup_confirmed=False,
            ai_provider="none", github=GitHubSummary(**github),
        )
    }


def test_health_line_for_github():
    assert checks()["GitHub"].status is HealthStatus.DISABLED
    assert checks(enabled=True, error="port occupé")["GitHub"].status is HealthStatus.ERROR
    ok = checks(enabled=True, listening=True, repository="ElBastiano7553/game-project")["GitHub"]
    assert ok.status is HealthStatus.OK and "game-project" in ok.detail and "non vérifié" in ok.detail
    assert "GitLab" in checks()  # la ligne GitLab est toujours là


# --- Cycle de vie avec le bot ----------------------------------------------------------


async def test_github_is_not_started_without_secret(db):
    bot = SaeBot(settings(), db)
    try:
        await bot.start_github_webhooks()
        assert bot.github_server is None and bot.github_error is None
    finally:
        await bot.close()


async def test_github_starts_and_stops_with_the_bot(db, unused_tcp_port):
    bot = SaeBot(settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, GITHUB_WEBHOOK_PORT=str(unused_tcp_port)), db)
    try:
        await bot.start_github_webhooks()
        assert bot.github_server is not None and bot.github_server.listening
        assert bot.github_error is None
        assert bot._github_task is not None
    finally:
        await bot.close()
    assert not bot.github_server.listening
    assert bot._github_task is None


async def test_gitlab_and_github_run_side_by_side(db, unused_tcp_port_factory):
    gitlab_port, github_port = unused_tcp_port_factory(), unused_tcp_port_factory()
    bot = SaeBot(
        settings(
            GITLAB_WEBHOOK_SECRET=GITLAB_SECRET,
            WEBHOOK_PORT=str(gitlab_port),
            GITHUB_WEBHOOK_SECRET=GITHUB_SECRET,
            GITHUB_WEBHOOK_PORT=str(github_port),
        ),
        db,
    )
    bot.wait_until_ready = asyncio.Event().wait  # pas de connexion Discord dans les tests
    try:
        await bot.start_gitlab_webhooks()
        await bot.start_github_webhooks()
        assert bot.gitlab_server.listening and bot.github_server.listening
        assert bot.gitlab_error is None and bot.github_error is None
    finally:
        await bot.close()
    assert not bot.gitlab_server.listening and not bot.github_server.listening


async def test_github_port_in_use_keeps_the_rest_of_the_bot(db, unused_tcp_port):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", unused_tcp_port))
    blocker.listen()
    bot = SaeBot(settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, GITHUB_WEBHOOK_PORT=str(unused_tcp_port)), db)
    try:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        await bot.start_github_webhooks()
        assert "impossible d'écouter" in bot.github_error
        assert len(bot.build_message_service().templates) == 10
    finally:
        blocker.close()
        await bot.close()


async def test_invalid_github_config_is_reported_without_starting(db):
    bot = SaeBot(settings(GITHUB_WEBHOOK_SECRET="court"), db)
    try:
        await bot.start_github_webhooks()
        assert bot.github_server is None
        assert "trop court" in bot.github_error
    finally:
        await bot.close()


async def test_unexpected_github_startup_error_keeps_the_bot(db, unused_tcp_port, monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("panne")

    monkeypatch.setattr("src.discord_bot.bot.create_github_app", explode)
    bot = SaeBot(settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, GITHUB_WEBHOOK_PORT=str(unused_tcp_port)), db)
    try:
        await bot.start_github_webhooks()
        assert "RuntimeError" in bot.github_error
    finally:
        await bot.close()


async def test_purge_history_includes_github(db):
    bot = SaeBot(settings(), db)
    try:
        assert "livraison(s) GitHub" in bot.purge_history()
    finally:
        await bot.close()


def test_github_notifications_use_the_existing_discord_embed():
    from src.discord_bot.embeds import notification_to_embed
    from src.github.events import format_event
    from src.github.samples import issue_payload, pull_request_payload, push_payload, workflow_run_payload

    for event, build in (("push", push_payload), ("pull_request", pull_request_payload), ("issues", issue_payload), ("workflow_run", workflow_run_payload)):
        embed = notification_to_embed(format_event(event, build()))
        assert embed.title and embed.footer.text == "GitHub · ElBastiano7553/game-project"
        assert len(embed) <= 6000
