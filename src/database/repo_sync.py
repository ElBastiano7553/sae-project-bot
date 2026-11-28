"""Commits et pull requests lus dans le dépôt GitHub par la synchronisation (table ``repo_sync_items``)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.database.connection import Database


@dataclass(frozen=True)
class SyncItem:
    repository: str
    kind: str  # commit, pull
    ref: str  # SHA complet ou numéro de pull request
    title: str
    author: str
    url: str | None
    state: str  # commit : « » ; pull request : open, closed, merged
    occurred_at: str | None  # ISO 8601 d'après GitHub
    refs: tuple[str, ...]  # éléments du suivi cités (FEAT-002…)

    @property
    def label(self) -> str:
        return f"#{self.ref}" if self.kind == "pull" else self.ref[:7]


def _item(r) -> SyncItem:
    return SyncItem(r["repository"], r["kind"], r["ref"], r["title"], r["author"], r["url"], r["state"], r["occurred_at"],
                    tuple(json.loads(r["refs"])))


class RepoSyncRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def record(self, item: SyncItem) -> tuple[bool, str | None]:
        """Enregistre l'élément. Renvoie (nouveau ?, ancien état) : un état qui change (PR fusionnée) est mis à jour."""
        with self._conn:
            row = self._conn.execute(
                "SELECT state FROM repo_sync_items WHERE repository = ? AND kind = ? AND ref = ?", (item.repository, item.kind, item.ref)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO repo_sync_items (repository, kind, ref, title, author, url, state, occurred_at, refs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (item.repository, item.kind, item.ref, item.title, item.author, item.url, item.state, item.occurred_at,
                     json.dumps(list(item.refs))),
                )
                return True, None
            self._conn.execute(
                """
                UPDATE repo_sync_items SET title = ?, state = ?, occurred_at = ?, refs = ?
                WHERE repository = ? AND kind = ? AND ref = ?
                """,
                (item.title, item.state, item.occurred_at, json.dumps(list(item.refs)), item.repository, item.kind, item.ref),
            )
            return False, row["state"]

    def mentioning(self, repository: str, key: str, limit: int = 25) -> list[SyncItem]:
        """Commits et pull requests qui citent l'élément ``key`` (plus récents d'abord)."""
        rows = self._conn.execute(
            """
            SELECT * FROM repo_sync_items
            WHERE repository = ? AND EXISTS (SELECT 1 FROM json_each(refs) WHERE value = ?)
            ORDER BY COALESCE(occurred_at, seen_at) DESC LIMIT ?
            """,
            (repository, key, limit),
        ).fetchall()
        return [_item(r) for r in rows]

    def count(self, repository: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM repo_sync_items WHERE repository = ? GROUP BY kind", (repository,)
        ).fetchall()
        return {r["kind"]: r["n"] for r in rows}
