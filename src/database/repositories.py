"""Dépôts de données : seules ces classes écrivent du SQL métier."""

from __future__ import annotations

from dataclasses import dataclass

from src.database.connection import Database


@dataclass(frozen=True)
class ManagedMessage:
    key: str
    channel_id: int
    message_id: int
    content_hash: str
    updated_at: str = ""


class ManagedMessageRepository:
    """Identifiants des messages permanents publiés par le bot."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, key: str) -> ManagedMessage | None:
        row = self._conn.execute(
            "SELECT key, channel_id, message_id, content_hash, updated_at FROM managed_messages WHERE key = ?",
            (key,),
        ).fetchone()
        return ManagedMessage(**dict(row)) if row else None

    def get_by_message_id(self, message_id: int) -> ManagedMessage | None:
        row = self._conn.execute(
            "SELECT key, channel_id, message_id, content_hash, updated_at FROM managed_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return ManagedMessage(**dict(row)) if row else None

    def list_all(self) -> list[ManagedMessage]:
        rows = self._conn.execute(
            "SELECT key, channel_id, message_id, content_hash, updated_at FROM managed_messages ORDER BY key"
        ).fetchall()
        return [ManagedMessage(**dict(row)) for row in rows]

    def upsert(self, key: str, channel_id: int, message_id: int, content_hash: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO managed_messages (key, channel_id, message_id, content_hash, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (key, channel_id, message_id, content_hash),
            )

    def delete(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM managed_messages WHERE key = ?", (key,))


class ChannelBindingRepository:
    """Association « fonction du bot » → identifiant du salon Discord."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get_all(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT key, channel_id FROM channel_bindings").fetchall()
        return {row["key"]: row["channel_id"] for row in rows}

    def replace_all(self, bindings: dict[str, int]) -> None:
        """Remplace toutes les associations en une seule transaction."""
        with self._conn:
            self._conn.execute("DELETE FROM channel_bindings")
            self._conn.executemany(
                "INSERT INTO channel_bindings (key, channel_id) VALUES (?, ?)",
                sorted(bindings.items()),
            )


class KeyValueRepository:
    """Petit stockage clé/valeur pour l'état du bot (ex. : setup confirmé)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


class GitLabEventRepository:
    """Événements GitLab déjà acceptés, identifiés par la clé de livraison fournie par GitLab.

    Statuts : ``queued`` (en attente d'envoi), ``sent`` (publié sur Discord),
    ``failed`` (envoi impossible), ``skipped`` (aucun salon associé).
    """

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def claim(self, event_key: str, event_type: str, notification: str | None = None) -> bool:
        """Enregistre l'événement. Retourne ``False`` s'il avait déjà été reçu (doublon).

        ``notification`` (JSON) permet de renvoyer la notification si le bot s'arrête avant l'envoi.
        """
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO gitlab_events (event_key, event_type, notification) VALUES (?, ?, ?)",
                (event_key, event_type, notification),
            )
        return cursor.rowcount == 1

    def pending(self, max_age_hours: int) -> list[tuple[str, str | None]]:
        """Événements acceptés mais jamais envoyés (bot arrêté entre-temps), du plus ancien au plus récent."""
        rows = self._conn.execute(
            """
            SELECT event_key, notification FROM gitlab_events
            WHERE status = 'queued' AND received_at >= datetime('now', ?)
            ORDER BY received_at, rowid
            """,
            (f"-{int(max_age_hours)} hours",),
        ).fetchall()
        return [(row["event_key"], row["notification"]) for row in rows]

    def release(self, event_key: str) -> None:
        """Oublie un événement qui n'a pas pu être pris en charge, pour que GitLab puisse le renvoyer."""
        with self._conn:
            self._conn.execute("DELETE FROM gitlab_events WHERE event_key = ?", (event_key,))

    def set_status(self, event_key: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE gitlab_events SET status = ?, updated_at = datetime('now') WHERE event_key = ?",
                (status, event_key),
            )

    def get_status(self, event_key: str) -> str | None:
        row = self._conn.execute("SELECT status FROM gitlab_events WHERE event_key = ?", (event_key,)).fetchone()
        return row["status"] if row else None

    def expire_queued(self, max_age_hours: int) -> int:
        """Abandonne les notifications trop anciennes pour être encore utiles (statut ``failed``)."""
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE gitlab_events SET status = 'failed', updated_at = datetime('now')
                WHERE status = 'queued' AND received_at < datetime('now', ?)
                """,
                (f"-{int(max_age_hours)} hours",),
            )
        return cursor.rowcount

    def last_received_at(self) -> str | None:
        row = self._conn.execute("SELECT MAX(received_at) AS last FROM gitlab_events").fetchone()
        return row["last"] if row else None

    def purge_older_than(self, days: int) -> int:
        """Supprime les événements anciens : GitLab ne renvoie jamais un événement après quelques jours."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM gitlab_events WHERE received_at < datetime('now', ?)", (f"-{int(days)} days",)
            )
        return cursor.rowcount
