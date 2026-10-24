"""Dépôt des projets suivis (tables ``projects`` et ``project_routes`` en v9, ``provider_links`` en v10).

Toutes les méthodes utilisées par les commandes prennent l'identifiant du serveur Discord :
un serveur ne peut ni lire ni modifier le projet d'un autre serveur, même en connaissant son numéro.

Seules exceptions, pour la réception des événements (étape B3) : ``github_targets`` et
``gitlab_target`` retrouvent les projets à partir de ce que le fournisseur envoie (installation
de la GitHub App, identifiant du webhook GitLab). Elles renvoient des ``ProjectRecord`` qui
portent leur ``guild_id`` : la suite (salons, filtres) passe par les méthodes ci-dessus avec
ce serveur-là, jamais un autre.
"""

from __future__ import annotations

import json
import re
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


@dataclass(frozen=True)
class ProviderLink:
    """Lien entre un projet et son fournisseur (créé par B4 ou B5). Aucun secret en clair."""

    project_id: int
    provider: str
    installation_id: int | None = None
    hook_id: str | None = None
    created_at: str = ""


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
HOOK_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


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

    def find(self, guild_id: int, provider: str, external_id: str, instance_url: str = "") -> ProjectRecord | None:
        """Projet déjà suivi par ce serveur (identifiants déjà normalisés), ou None."""
        row = self._conn.execute(
            "SELECT * FROM projects WHERE guild_id = ? AND provider = ? AND instance_url = ? AND external_id = ?",
            (guild_id, provider, instance_url, external_id),
        ).fetchone()
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

    # --- Liens avec le fournisseur (v10) ----------------------------------------------------------

    def link_github(self, guild_id: int, project_id: int, installation_id: int) -> None:
        project = self.get(guild_id, project_id)
        if project is None or project.provider != "github":
            raise ProjectError("Projet GitHub introuvable sur ce serveur.")
        if not isinstance(installation_id, int) or isinstance(installation_id, bool) or installation_id <= 0:
            raise ProjectError("Numéro d'installation GitHub invalide.")
        self._save_link(project_id, "github", installation_id, None, None)

    def link_gitlab(self, guild_id: int, project_id: int, hook_id: str, secret_hash: str) -> None:
        """``secret_hash`` : SHA-256 hexadécimal du secret (``services.projects.hash_secret``), jamais le secret."""
        project = self.get(guild_id, project_id)
        if project is None or project.provider != "gitlab":
            raise ProjectError("Projet GitLab introuvable sur ce serveur.")
        if not HOOK_ID.match(hook_id or "") or not _SHA256_HEX.match(secret_hash or ""):
            raise ProjectError("Identifiant de webhook ou empreinte de secret invalide.")
        self._save_link(project_id, "gitlab", None, hook_id, secret_hash)

    def _save_link(self, project_id: int, provider: str, installation_id, hook_id, secret_hash) -> None:
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO provider_links (project_id, provider, installation_id, hook_id, secret_hash) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (project_id) DO UPDATE SET installation_id = excluded.installation_id,
                        hook_id = excluded.hook_id, secret_hash = excluded.secret_hash, created_at = datetime('now')
                    """,
                    (project_id, provider, installation_id, hook_id, secret_hash),
                )
        except sqlite3.IntegrityError as exc:
            raise ProjectError("Cet identifiant de webhook est déjà utilisé.") from exc

    def unlink(self, guild_id: int, project_id: int) -> bool:
        if self.get(guild_id, project_id) is None:
            return False
        with self._conn:
            cursor = self._conn.execute("DELETE FROM provider_links WHERE project_id = ?", (project_id,))
        return cursor.rowcount == 1

    def link(self, guild_id: int, project_id: int) -> ProviderLink | None:
        row = self._conn.execute(
            """
            SELECT l.* FROM provider_links l JOIN projects p ON p.id = l.project_id
            WHERE l.project_id = ? AND p.guild_id = ?
            """,
            (project_id, guild_id),
        ).fetchone()
        if row is None:
            return None
        return ProviderLink(row["project_id"], row["provider"], row["installation_id"], row["hook_id"], row["created_at"])

    def linked_ids(self, guild_id: int) -> frozenset[int]:
        rows = self._conn.execute(
            "SELECT l.project_id FROM provider_links l JOIN projects p ON p.id = l.project_id WHERE p.guild_id = ?", (guild_id,)
        )
        return frozenset(row["project_id"] for row in rows)

    # --- Réception (B3) : à partir de ce que le fournisseur envoie -------------------------------

    def github_targets(self, installation_id: int, repository: str) -> list[ProjectRecord]:
        """Projets actifs reliés à CETTE installation de la GitHub App et suivant CE dépôt (tous serveurs)."""
        rows = self._conn.execute(
            """
            SELECT p.* FROM projects p JOIN provider_links l ON l.project_id = p.id
            WHERE l.provider = 'github' AND l.installation_id = ? AND p.provider = 'github'
              AND p.external_id = ? AND p.status = 'active'
            ORDER BY p.id
            """,
            (installation_id, repository.casefold()),
        )
        return [_record(row) for row in rows]

    def gitlab_target(self, hook_id: str) -> tuple[ProjectRecord, str] | None:
        """Projet actif relié à ce webhook GitLab, avec l'empreinte du secret attendu."""
        row = self._conn.execute(
            """
            SELECT p.*, l.secret_hash FROM projects p JOIN provider_links l ON l.project_id = p.id
            WHERE l.provider = 'gitlab' AND l.hook_id = ? AND p.status = 'active'
            """,
            (hook_id,),
        ).fetchone()
        return (_record(row), row["secret_hash"]) if row else None
