"""Livraisons reçues pour les projets enregistrés (table ``project_deliveries``, migration v10).

Une ligne par projet et par livraison : le même événement reçu pour deux projets (deux serveurs
qui suivent le même dépôt) donne deux lignes indépendantes. Les tables ``gitlab_events`` et
``github_deliveries`` restent réservées aux projets historiques du ``.env``.

Statuts : ``queued`` (en attente d'envoi), ``sent`` (publié sur Discord), ``failed`` (envoi
impossible), ``skipped`` (salon refusé ou introuvable), ``filtered`` (écarté par les filtres du
projet, gardé pour l'anti-doublons).
"""

from __future__ import annotations

from src.database.connection import Database


class ProjectDeliveryRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def claim(
        self,
        project_id: int,
        guild_id: int,
        delivery_key: str,
        category: str,
        *,
        dedup_key: str | None,
        channel_id: int | None,
        notification: str | None,
        status: str = "queued",
    ) -> bool:
        """Enregistre la livraison pour ce projet. ``False`` si elle (ou le même fait) était déjà reçue.

        La clé primaire écarte une livraison renvoyée (même identifiant) ; l'index unique sur
        ``(project_id, dedup_key)`` écarte le même fait livré sous un autre identifiant.
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO project_deliveries
                    (project_id, guild_id, delivery_key, dedup_key, category, channel_id, status, notification)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, guild_id, delivery_key, dedup_key, category, channel_id, status, notification),
            )
        return cursor.rowcount == 1

    def release(self, project_id: int, delivery_key: str) -> None:
        """Oublie une livraison qui n'a pas pu être mise en file, pour que le fournisseur puisse la renvoyer."""
        with self._conn:
            self._conn.execute("DELETE FROM project_deliveries WHERE project_id = ? AND delivery_key = ?", (project_id, delivery_key))

    def set_status(self, project_id: int, delivery_key: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE project_deliveries SET status = ?, updated_at = datetime('now') WHERE project_id = ? AND delivery_key = ?",
                (status, project_id, delivery_key),
            )

    def get_status(self, project_id: int, delivery_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM project_deliveries WHERE project_id = ? AND delivery_key = ?", (project_id, delivery_key)
        ).fetchone()
        return row["status"] if row else None

    def pending(self, max_age_hours: int) -> list[tuple[int, int, str, int | None, str | None]]:
        """Livraisons acceptées mais jamais envoyées : (projet, serveur, clé, salon, notification JSON)."""
        rows = self._conn.execute(
            """
            SELECT project_id, guild_id, delivery_key, channel_id, notification FROM project_deliveries
            WHERE status = 'queued' AND received_at >= datetime('now', ?)
            ORDER BY received_at, rowid
            """,
            (f"-{int(max_age_hours)} hours",),
        ).fetchall()
        return [(row["project_id"], row["guild_id"], row["delivery_key"], row["channel_id"], row["notification"]) for row in rows]

    def expire_queued(self, max_age_hours: int) -> int:
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE project_deliveries SET status = 'failed', updated_at = datetime('now')
                WHERE status = 'queued' AND received_at < datetime('now', ?)
                """,
                (f"-{int(max_age_hours)} hours",),
            )
        return cursor.rowcount

    def last_received_at(self) -> str | None:
        row = self._conn.execute("SELECT MAX(received_at) AS last FROM project_deliveries").fetchone()
        return row["last"] if row else None

    def project_summary(self, project_id: int, days: int) -> tuple[dict[str, int], str | None]:
        """Livraisons de CE projet par statut sur ``days`` jours, et date de la dernière (``/projet diagnostic``)."""
        rows = self._conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM project_deliveries
            WHERE project_id = ? AND received_at >= datetime('now', ?) GROUP BY status
            """,
            (project_id, f"-{int(days)} days"),
        ).fetchall()
        last = self._conn.execute("SELECT MAX(received_at) AS last FROM project_deliveries WHERE project_id = ?", (project_id,)).fetchone()
        return {row["status"]: row["n"] for row in rows}, last["last"]

    def purge_older_than(self, days: int) -> int:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM project_deliveries WHERE received_at < datetime('now', ?)", (f"-{int(days)} days",))
        return cursor.rowcount
