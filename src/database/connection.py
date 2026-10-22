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
    # Version 3 : contenu de la notification, pour la renvoyer après un arrêt du bot.
    """
    ALTER TABLE gitlab_events ADD COLUMN notification TEXT;
    """,
    # Version 4 : suivi de l'état des messages permanents, résumé consultable des événements
    # GitLab (avec empreinte anti-doublon) et journal des actions d'administration.
    # Uniquement des ajouts : aucune donnée existante n'est modifiée ni supprimée.
    """
    ALTER TABLE managed_messages ADD COLUMN last_action TEXT;
    ALTER TABLE managed_messages ADD COLUMN last_error TEXT;
    ALTER TABLE managed_messages ADD COLUMN last_checked_at TEXT;

    ALTER TABLE gitlab_events ADD COLUMN dedup_key TEXT;
    ALTER TABLE gitlab_events ADD COLUMN action TEXT;
    ALTER TABLE gitlab_events ADD COLUMN ref TEXT;
    ALTER TABLE gitlab_events ADD COLUMN title TEXT;
    ALTER TABLE gitlab_events ADD COLUMN url TEXT;
    ALTER TABLE gitlab_events ADD COLUMN author TEXT;
    ALTER TABLE gitlab_events ADD COLUMN occurred_at TEXT;
    CREATE UNIQUE INDEX gitlab_events_dedup ON gitlab_events (dedup_key) WHERE dedup_key IS NOT NULL;
    CREATE INDEX gitlab_events_type_received ON gitlab_events (event_type, received_at);
    -- Les événements déjà reçus en V2 gardent leur titre et leur lien dans l'historique.
    UPDATE gitlab_events
       SET title = json_extract(notification, '$.title'), url = json_extract(notification, '$.url')
     WHERE notification IS NOT NULL AND json_valid(notification);

    CREATE TABLE admin_actions (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        at        TEXT    NOT NULL DEFAULT (datetime('now')),
        user_id   INTEGER NOT NULL,
        user_name TEXT    NOT NULL,
        action    TEXT    NOT NULL,
        target    TEXT    NOT NULL DEFAULT '',
        outcome   TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX admin_actions_at ON admin_actions (at);
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
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"La base {self.path} est en version {current}, plus récente que ce bot ({len(MIGRATIONS)}) : "
                "mettez le bot à jour au lieu d'utiliser une ancienne version."
            )
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            log.info("Migration de la base de données vers la version %d", version)
            # Une migration est appliquée entièrement ou pas du tout : en cas d'erreur, la base
            # reste dans sa version précédente, intacte, et la migration sera retentée au démarrage.
            try:
                self.conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
            except sqlite3.Error:
                if self.conn.in_transaction:
                    self.conn.rollback()
                log.exception("Échec de la migration vers la version %d : base laissée en version %d", version, version - 1)
                raise

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
