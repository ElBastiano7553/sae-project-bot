"""Dépôt des projets suivis (tables ``projects`` et ``project_routes``, migration v9).

Toutes les méthodes prennent l'identifiant du serveur Discord : un serveur ne peut
ni lire ni modifier le projet d'un autre serveur, même en connaissant son numéro.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from src.database.connection import Database
from src.services.projects import (
    CATEGORIES,
    ProjectError,
    Route,
    check_category,
    check_provider,
    normalize_external_id,
    normalize_instance_url,
    normalize_name,
)


@dataclass(frozen=True)
class ProjectRecord:
    id: int
    guild_id: int
    provider: str
    external_id: str
    instance_url: str
    name: str
    status: str
    created_by: int
    created_at: str


def _record(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        guild_id=row["guild_id"],
        provider=row["provider"],
        external_id=row["external_id"],
        instance_url=row["instance_url"],
        name=row["name"],
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


class ProjectRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def create(
        self, guild_id: int, provider: str, external_id: str, name: str, *, created_by: int, instance_url: str = ""
    ) -> ProjectRecord:
        provider = check_provider(provider)
        external_id = normalize_external_id(provider, external_id)
        instance_url = normalize_instance_url(provider, instance_url)
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "INSERT INTO projects (guild_id, provider, external_id, instance_url, name, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                    (guild_id, provider, external_id, instance_url, normalize_name(name), created_by),
                )
        except sqlite3.IntegrityError as exc:
            raise ProjectError("Ce projet est déjà suivi sur ce serveur.") from exc
        project = self.get(guild_id, cursor.lastrowid)
        assert project is not None
        return project

    def get(self, guild_id: int, project_id: int) -> ProjectRecord | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ? AND guild_id = ?", (project_id, guild_id)).fetchone()
        return _record(row) if row else None

    def list_for_guild(self, guild_id: int) -> list[ProjectRecord]:
        rows = self._conn.execute("SELECT * FROM projects WHERE guild_id = ? ORDER BY name COLLATE NOCASE, id", (guild_id,))
        return [_record(row) for row in rows]

    def delete(self, guild_id: int, project_id: int) -> bool:
        """Supprime le projet et ses salons (ON DELETE CASCADE). ``False`` s'il n'est pas à ce serveur."""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM projects WHERE id = ? AND guild_id = ?", (project_id, guild_id))
        return cursor.rowcount == 1

    def set_route(self, guild_id: int, project_id: int, category: str, channel_id: int, filters: dict | None = None) -> None:
        """Associe un salon à une catégorie. Le salon doit déjà avoir été vérifié dans ce serveur."""
        check_category(category)
        if self.get(guild_id, project_id) is None:
            raise ProjectError("Projet introuvable sur ce serveur.")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO project_routes (project_id, category, channel_id, filters) VALUES (?, ?, ?, ?)
                ON CONFLICT (project_id, category)
                DO UPDATE SET channel_id = excluded.channel_id, filters = excluded.filters, updated_at = datetime('now')
                """,
                (project_id, category, channel_id, json.dumps(filters or {}, sort_keys=True)),
            )

    def remove_route(self, guild_id: int, project_id: int, category: str) -> bool:
        if self.get(guild_id, project_id) is None:
            return False
        with self._conn:
            cursor = self._conn.execute("DELETE FROM project_routes WHERE project_id = ? AND category = ?", (project_id, category))
        return cursor.rowcount == 1

    def routes(self, guild_id: int, project_id: int) -> tuple[Route, ...]:
        rows = self._conn.execute(
            """
            SELECT r.category, r.channel_id, r.filters FROM project_routes r
            JOIN projects p ON p.id = r.project_id
            WHERE r.project_id = ? AND p.guild_id = ?
            """,
            (project_id, guild_id),
        ).fetchall()
        order = list(CATEGORIES)
        routes = [Route(row["category"], row["channel_id"], json.loads(row["filters"])) for row in rows]
        return tuple(sorted(routes, key=lambda r: order.index(r.category)))
