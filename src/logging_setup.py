"""Configuration des journaux : console + fichier logs/bot.log (rotation automatique)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.services.monitoring import ErrorTrackingHandler, RecentErrors, SecretRedactingFilter

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(
    level: str,
    log_dir: Path,
    *,
    secrets: Iterable[str] = (),
    errors: RecentErrors | None = None,
) -> None:
    """``secrets`` : valeurs à masquer dans tous les journaux. ``errors`` : reçoit les erreurs pour /status."""
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    redactor = SecretRedactingFilter(secrets)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in (console, file_handler):
        handler.addFilter(redactor)
        root.addHandler(handler)
    if errors is not None:
        root.addHandler(ErrorTrackingHandler(errors, secrets))
    # discord.py est très bavard en DEBUG : on garde ses messages à partir de INFO.
    logging.getLogger("discord").setLevel(max(logging.INFO, logging.getLevelName(level)))
