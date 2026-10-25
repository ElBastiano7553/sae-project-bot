"""Dépôt de la musique : historique des lectures, favoris et volume par serveur (migration v13).

Chaque requête est filtrée par ``guild_id`` : un serveur ne lit jamais l'historique ni les
favoris d'un autre. Chaque écriture est une transaction courte (``with self._conn``) :
interrompue, elle n'écrit rien.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.database.connection import Database
from src.music.models import Track

# Favoris d'une personne dans un serveur : au-delà, il faut en retirer.
MAX_FAVORITES = 100


@dataclass(frozen=True)
class HistoryRow:
    id: int
    track_key: str
    uri: str | None
    title: str
    author: str
    source: str
    length_ms: int
    requested_by: int
    played_at: str


@dataclass(frozen=True)
class FavoriteRow:
    track_key: str
    uri: str
    title: str
    author: str
    source: str
    length_ms: int
    created_at: str


class FavoritesFull(Exception):
    pass


class MusicRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    # --- Historique --------------------------------------------------------------

    def record_play(self, guild_id: int, track: Track, requested_by: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO music_history (guild_id, track_key, uri, title, author, source, length_ms, requested_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, track.key, track.uri, track.title, track.author, track.source, track.length_ms, requested_by),
            )

    def history(self, guild_id: int, *, limit: int = 10, offset: int = 0) -> list[HistoryRow]:
        rows = self._conn.execute(
            "SELECT id, track_key, uri, title, author, source, length_ms, requested_by, played_at FROM music_history "
            "WHERE guild_id = ? ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ).fetchall()
        return [HistoryRow(**dict(row)) for row in rows]

    def history_count(self, guild_id: int) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM music_history WHERE guild_id = ?", (guild_id,)).fetchone()[0])

    def purge_older_than(self, days: int) -> int:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM music_history WHERE played_at < datetime('now', ?)", (f"-{int(days)} days",))
        return cursor.rowcount

    # --- Favoris -----------------------------------------------------------------

    def is_favorite(self, guild_id: int, user_id: int, track_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM music_favorites WHERE guild_id = ? AND user_id = ? AND track_key = ?", (guild_id, user_id, track_key)
        ).fetchone()
        return row is not None

    def add_favorite(self, guild_id: int, user_id: int, track: Track) -> bool:
        """Ajoute un favori. Retourne False s'il y était déjà (aucun doublon). Lève ``FavoritesFull``."""
        if not track.uri:
            raise ValueError("une piste sans lien ne peut pas être mise en favori")
        with self._conn:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM music_favorites WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            ).fetchone()[0]
            try:
                if count >= MAX_FAVORITES:
                    if self.is_favorite(guild_id, user_id, track.key):
                        return False
                    raise FavoritesFull
                self._conn.execute(
                    "INSERT INTO music_favorites (guild_id, user_id, track_key, uri, title, author, source, length_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (guild_id, user_id, track.key, track.uri, track.title, track.author, track.source, track.length_ms),
                )
            except sqlite3.IntegrityError:
                return False  # déjà en favori (clé primaire)
        return True

    def remove_favorite(self, guild_id: int, user_id: int, track_key: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM music_favorites WHERE guild_id = ? AND user_id = ? AND track_key = ?", (guild_id, user_id, track_key)
            )
        return cursor.rowcount > 0

    def favorites(self, guild_id: int, user_id: int) -> list[FavoriteRow]:
        """Favoris d'une personne dans ce serveur, du plus ancien au plus récent (numérotation stable)."""
        rows = self._conn.execute(
            "SELECT track_key, uri, title, author, source, length_ms, created_at FROM music_favorites "
            "WHERE guild_id = ? AND user_id = ? ORDER BY created_at, rowid",
            (guild_id, user_id),
        ).fetchall()
        return [FavoriteRow(**dict(row)) for row in rows]

    # --- Réglages du serveur ---------------------------------------------------------

    def volume(self, guild_id: int) -> int | None:
        row = self._conn.execute("SELECT volume FROM music_guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        return int(row["volume"]) if row else None

    def set_volume(self, guild_id: int, volume: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO music_guild_settings (guild_id, volume) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET volume = excluded.volume, updated_at = datetime('now')",
                (guild_id, volume),
            )
