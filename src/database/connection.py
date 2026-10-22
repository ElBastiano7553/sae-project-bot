"""Connexion SQLite et migrations du schéma.

Le schéma est versionné avec ``PRAGMA user_version`` : chaque entrée de
``MIGRATIONS`` est appliquée une seule fois, dans l'ordre. Pour faire évoluer
la base (par exemple en V2 pour les événements GitLab déjà traités), on ajoute
une nouvelle migration à la fin de la liste, sans modifier les précédentes.

SQLite est utilisé de façon synchrone : les requêtes du bot sont minuscules
(quelques lignes) et durent bien moins d'une milliseconde, ce qui ne bloque
pas la boucle asynchrone de Discord de façon perceptible.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # Version 1 : messages permanents, associations de salons, état clé/valeur.
    """
    CREATE TABLE managed_messages (
        key          TEXT PRIMARY KEY,
        channel_id   INTEGER NOT NULL,
        message_id   INTEGER NOT NULL,
        content_hash TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE channel_bindings (
        key        TEXT PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE kv_store (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # Version 2 : événements GitLab déjà reçus (évite les notifications en double).
    """
    CREATE TABLE gitlab_events (
        event_key   TEXT PRIMARY KEY,
        event_type  TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'queued',
        received_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX gitlab_events_received_at ON gitlab_events (received_at);
    """,
]


class Database:
    """Petite enveloppe autour de ``sqlite3`` qui applique les migrations à l'ouverture."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            log.info("Migration de la base de données vers la version %d", version)
            with self.conn:
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {version}")

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def ping(self) -> bool:
        """Vérifie que la base répond (utilisé par /health)."""
        try:
            self.conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            log.exception("La base de données ne répond pas")
            return False
        return True

    def close(self) -> None:
        self.conn.close()
