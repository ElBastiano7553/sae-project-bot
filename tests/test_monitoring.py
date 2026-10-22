"""Erreurs récentes et masquage des secrets dans les journaux."""

import datetime as dt
import logging

from src.config import secret_values
from src.services.monitoring import ErrorTrackingHandler, RecentErrors, SecretRedactingFilter, redact

SECRET = "jeton-ultra-secret-0123456789"


def test_redact_masks_known_secrets_only():
    assert redact(f"token={SECRET} ok", [SECRET, ""]) == "token=*** ok"
    assert redact("bonjour", ["bon"]) == "bonjour"  # trop court pour être masqué sans risque


def test_recent_errors_keep_one_line_and_bounded_history():
    errors = RecentErrors(maxlen=2)
    errors.record("a", "première\nTraceback (most recent call last): …")
    errors.record("b", "deuxième")
    errors.record("c", "troisième", at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    assert errors.total == 3
    assert errors.last.message == "troisième"
    assert [e.source for e in errors.items()] == ["c", "b"]
    assert RecentErrors().last is None


def test_logging_filter_masks_secrets_in_messages_and_tracebacks(caplog):
    logger = logging.getLogger("test.redaction")
    redactor = SecretRedactingFilter([SECRET])
    caplog.handler.addFilter(redactor)
    try:
        logger.error("connexion avec %s", SECRET)
        try:
            raise ValueError(f"mauvais jeton {SECRET}")
        except ValueError:
            logger.exception("échec")
    finally:
        caplog.handler.removeFilter(redactor)
    assert SECRET not in caplog.text
    assert "connexion avec ***" in caplog.text


def test_error_tracking_handler_ignores_warnings_and_redacts():
    errors = RecentErrors()
    logger = logging.getLogger("test.tracking")
    handler = ErrorTrackingHandler(errors, [SECRET])
    logger.addHandler(handler)
    try:
        logger.warning("simple avertissement")
        logger.error("refus avec %s", SECRET)
    finally:
        logger.removeHandler(handler)
    assert errors.total == 1
    assert errors.last.message == "refus avec ***"
    assert errors.last.source == "test.tracking"


def test_secret_values_reads_only_secret_variables():
    env = {"DISCORD_BOT_TOKEN": " abc ", "AI_API_KEY": "", "DISCORD_GUILD_ID": "42"}
    assert secret_values(env) == ["abc"]
