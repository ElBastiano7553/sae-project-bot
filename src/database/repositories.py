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
