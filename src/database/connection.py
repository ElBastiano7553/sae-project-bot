"""Connexion SQLite et migrations du schéma.

Le schéma est versionné avec ``PRAGMA user_version`` : chaque entrée de
``MIGRATIONS`` est appliquée une seule fois, dans l'ordre. Pour faire évoluer
la base (par exemple en V2 pour les événements GitLab déjà traités), on ajoute
une nouvelle migration à la fin de la liste, sans modifier les précédentes.

SQLite est utilisé de façon synchrone : les requêtes du bot sont minuscules
(quelques lignes) et durent bien moins d'une milliseconde, ce qui ne bloque
pas la boucle asynchrone de Discord de façon perceptible.

Avant toute migration d'une base existante, une sauvegarde vérifiée est créée
(``data/backups/sae_bot-…-avantvN.sqlite3``) ; si elle échoue, la migration n'a
pas lieu et le bot ne démarre pas, plutôt que de modifier la base sans filet.
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
    # Version 5 : historique exploitable pour les statistiques (lot 2). Tous les événements
    # authentifiés utiles sont conservés, même ceux qui ne sont pas notifiés (statut « recorded »).
    # Uniquement des ajouts ; les lignes existantes reçoivent une date d'événement calculée.
    """
    ALTER TABLE gitlab_events ADD COLUMN source TEXT NOT NULL DEFAULT 'webhook';
    ALTER TABLE gitlab_events ADD COLUMN event_at TEXT;
    ALTER TABLE gitlab_events ADD COLUMN user_id INTEGER;
    ALTER TABLE gitlab_events ADD COLUMN subject_id INTEGER;
    ALTER TABLE gitlab_events ADD COLUMN subject_author_id INTEGER;
    ALTER TABLE gitlab_events ADD COLUMN commit_count INTEGER;
    ALTER TABLE gitlab_events ADD COLUMN environment TEXT;
    UPDATE gitlab_events SET event_at = COALESCE(datetime(occurred_at), received_at);
    CREATE INDEX gitlab_events_type_event_at ON gitlab_events (event_type, event_at);
    CREATE INDEX gitlab_events_user ON gitlab_events (user_id, event_at);

    CREATE TABLE gitlab_users (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        username   TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE bot_errors (
        fingerprint TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        message     TEXT NOT NULL,
        first_at    TEXT NOT NULL DEFAULT (datetime('now')),
        last_at     TEXT NOT NULL DEFAULT (datetime('now')),
        count       INTEGER NOT NULL DEFAULT 1,
        alerted_at  TEXT
    );
    CREATE INDEX bot_errors_last_at ON bot_errors (last_at);
    """,
    # v6 (lot 3) : journal des demandes à l'IA (quotas, suivi des coûts, journal IA du SAE).
    # Le texte des questions et des réponses n'est pas conservé.
    """
    CREATE TABLE ai_requests (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        at            TEXT NOT NULL DEFAULT (datetime('now')),
        user_id       INTEGER NOT NULL,
        command       TEXT NOT NULL,
        model         TEXT,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL
    );
    CREATE INDEX ai_requests_at ON ai_requests (at);
    CREATE INDEX ai_requests_user ON ai_requests (user_id, at);
    """,
    # v7 (Samae 2.0, lot 1) : réunions, participants, ordre du jour, décisions, réserves,
    # actions et historique. Uniquement des créations de tables : rien d'existant n'est modifié.
    # ``revision`` augmente à chaque modification : un bouton d'un panneau périmé est refusé.
    """
    CREATE TABLE meetings (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        title              TEXT    NOT NULL,
        description        TEXT    NOT NULL DEFAULT '',
        objectives         TEXT    NOT NULL DEFAULT '',
        leader_id          INTEGER NOT NULL,
        leader_name        TEXT    NOT NULL,
        status             TEXT    NOT NULL DEFAULT 'planned'
                                   CHECK (status IN ('planned', 'running', 'finished', 'cancelled')),
        planned_minutes    INTEGER,
        channel_id         INTEGER,
        panel_message_id   INTEGER,
        report             TEXT,
        report_channel_id  INTEGER,
        report_message_ids TEXT,
        revision           INTEGER NOT NULL DEFAULT 0,
        created_by         INTEGER NOT NULL,
        created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
        started_at         TEXT,
        ended_at           TEXT,
        updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX meetings_status ON meetings (status, id);
    CREATE INDEX meetings_channel ON meetings (channel_id, status);

    CREATE TABLE meeting_participants (
        meeting_id INTEGER NOT NULL REFERENCES meetings (id),
        user_id    INTEGER NOT NULL,
        user_name  TEXT    NOT NULL,
        joined_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (meeting_id, user_id)
    );
    CREATE INDEX meeting_participants_user ON meeting_participants (user_id);

    CREATE TABLE meeting_steps (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL REFERENCES meetings (id),
        position   INTEGER NOT NULL,
        title      TEXT    NOT NULL,
        status     TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'current', 'done')),
        added_by   INTEGER NOT NULL,
        created_at TEXT    NOT NULL DEFAULT (datetime('now')),
        started_at TEXT,
        done_at    TEXT,
        UNIQUE (meeting_id, position)
    );
    -- Au plus une étape en cours par réunion, garanti par la base elle-même.
    CREATE UNIQUE INDEX meeting_steps_current ON meeting_steps (meeting_id) WHERE status = 'current';

    CREATE TABLE meeting_notes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id    INTEGER NOT NULL REFERENCES meetings (id),
        kind          TEXT    NOT NULL CHECK (kind IN ('decision', 'reserve', 'disagreement')),
        content       TEXT    NOT NULL,
        rationale     TEXT,
        step_position INTEGER,
        author_id     INTEGER NOT NULL,
        author_name   TEXT    NOT NULL,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX meeting_notes_meeting ON meeting_notes (meeting_id, id);

    CREATE TABLE meeting_actions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id    INTEGER NOT NULL REFERENCES meetings (id),
        description   TEXT    NOT NULL,
        assignee_id   INTEGER NOT NULL,
        assignee_name TEXT    NOT NULL,
        due_date      TEXT,
        status        TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
        created_by    INTEGER NOT NULL,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX meeting_actions_meeting ON meeting_actions (meeting_id, id);
    CREATE INDEX meeting_actions_assignee ON meeting_actions (assignee_id, status);

    CREATE TABLE meeting_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL REFERENCES meetings (id),
        at         TEXT    NOT NULL DEFAULT (datetime('now')),
        user_id    INTEGER NOT NULL,
        user_name  TEXT    NOT NULL,
        action     TEXT    NOT NULL,
        detail     TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX meeting_log_meeting ON meeting_log (meeting_id, id);
    """,
    # v8 : livraisons des webhooks GitHub (dépôt du jeu). Table séparée de gitlab_events :
    # /activite stats, /activite contributions, /activite resume et le tableau de bord ne lisent que des données GitLab.
    # Uniquement une création de table : rien d'existant n'est modifié.
    """
    CREATE TABLE github_deliveries (
        delivery_key TEXT PRIMARY KEY,  -- « <événement>:<X-GitHub-Delivery> »
        event_type   TEXT NOT NULL,
        status       TEXT NOT NULL,
        dedup_key    TEXT,              -- empreinte du fait GitHub (même fait livré deux fois)
        notification TEXT,              -- JSON, pour renvoyer après un redémarrage
        title        TEXT,
        url          TEXT,
        received_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX github_deliveries_dedup ON github_deliveries (dedup_key) WHERE dedup_key IS NOT NULL;
    CREATE INDEX github_deliveries_received_at ON github_deliveries (received_at);
    """,
]


# Sauvegardes faites avant une migration : conservées plus longtemps que les sauvegardes régulières.
PRE_MIGRATION_KEEP = 30


class Database:
    """Petite enveloppe autour de ``sqlite3`` qui applique les migrations à l'ouverture."""

    def __init__(self, path: Path | str, *, backup_dir: Path | None = None) -> None:
        self.path = str(path)
        self.in_memory = self.path == ":memory:"
        if not self.in_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir = backup_dir or (None if self.in_memory else Path(self.path).parent / "backups")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Une écriture concurrente (sauvegarde, outil externe) attend au lieu d'échouer aussitôt.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if not self.in_memory:
            # WAL : lectures pendant les écritures, meilleure résistance à un arrêt brutal.
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"La base {self.path} est en version {current}, plus récente que ce bot ({len(MIGRATIONS)}) : "
                "mettez le bot à jour au lieu d'utiliser une ancienne version."
            )
        if 0 < current < len(MIGRATIONS) and self.backup_dir is not None:
            from src.services.backup import create_backup  # import local : pas de dépendance circulaire

            info = create_backup(self.conn, self.backup_dir, keep=PRE_MIGRATION_KEEP, reason=f"avantv{len(MIGRATIONS)}")
            log.info("Sauvegarde avant migration (v%d → v%d) : %s", current, len(MIGRATIONS), info.path)
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

    def integrity_ok(self) -> bool:
        """Vérification rapide de la base (``PRAGMA quick_check``), faite au démarrage."""
        try:
            result = self.conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error:
            log.exception("Vérification de la base impossible")
            return False
        if result != "ok":
            log.error("La base %s est endommagée (%s) : restaurez une sauvegarde (docs/deployment-linux.md)", self.path, result)
        return result == "ok"

    def ping(self) -> bool:
        """Vérifie que la base répond (utilisé par /etat)."""
        try:
            self.conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            log.exception("La base de données ne répond pas")
            return False
        return True

    def close(self) -> None:
        self.conn.close()
