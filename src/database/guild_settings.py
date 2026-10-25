"""Réglages propres à CHAQUE serveur Discord, choisis depuis Discord avec /setup (migration v14).

Rien ici n'est global : chaque ligne appartient à un serveur (clé primaire ``guild_id``), si
bien qu'un réglage d'un serveur n'affecte jamais un autre serveur. Les réglages de la machine
(token, Lavalink, limites) restent dans ``.env``.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.database.connection import Database


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    music_dj_role_id: int | None = None  # rôle « DJ » de ce serveur (prioritaire sur MUSIC_DJ_ROLE_NAME)


class GuildSettingsRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, guild_id: int) -> GuildSettings:
        row = self._conn.execute("SELECT music_dj_role_id FROM guild_settings WHERE guild_id = ?", (int(guild_id),)).fetchone()
        return GuildSettings(int(guild_id), row["music_dj_role_id"] if row else None)

    def set_dj_role(self, guild_id: int, role_id: int | None) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, music_dj_role_id) VALUES (?, ?)
                ON CONFLICT (guild_id) DO UPDATE SET music_dj_role_id = excluded.music_dj_role_id, updated_at = datetime('now')
                """,
                (int(guild_id), None if role_id is None else int(role_id)),
            )

    def forget(self, guild_id: int) -> None:
        """Le bot a quitté ce serveur : ses réglages ne servent plus."""
        with self._conn:
            self._conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (int(guild_id),))
