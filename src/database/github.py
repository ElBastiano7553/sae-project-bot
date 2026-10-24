"""Dépôt des livraisons de webhooks GitHub (table ``github_deliveries``, migration v8).

Volontairement séparé de ``gitlab_events`` : l'historique GitLab alimente /activite stats,
/activite contributions, /activite resume et le tableau de bord, qui ne savent pas lire des données GitHub.

Statuts : ``queued`` (en attente d'envoi), ``sent`` (publié sur Discord),
``failed`` (envoi impossible), ``skipped`` (salon non associé).
"""

from __future__ import annotations

from src.database.connection import Database


class GitHubDeliveryRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def claim(
        self,
        delivery_key: str,
        event_type: str,
        notification: str,
        *,
        dedup_key: str | None = None,
        title: str = "",
        url: str | None = None,
    ) -> bool:
        """Enregistre la livraison. ``False`` si elle (ou le même fait GitHub) avait déjà été reçue.

        La clé primaire écarte une livraison renvoyée par GitHub (même ``X-GitHub-Delivery``) ;
        l'index unique sur ``dedup_key`` écarte le même fait livré sous un autre identifiant.
        Les deux vérifications tiennent en une seule insertion atomique.
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO github_deliveries
                    (delivery_key, event_type, status, dedup_key, notification, title, url)
                VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (delivery_key, event_type, dedup_key, notification, title or None, url),
            )
        return cursor.rowcount == 1

    def release(self, delivery_key: str) -> None:
        """Oublie une livraison qui n'a pas pu être prise en charge, pour que GitHub puisse la renvoyer."""
        with self._conn:
            self._conn.execute("DELETE FROM github_deliveries WHERE delivery_key = ?", (delivery_key,))

    def set_status(self, delivery_key: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE github_deliveries SET status = ?, updated_at = datetime('now') WHERE delivery_key = ?",
                (status, delivery_key),
            )

    def get_status(self, delivery_key: str) -> str | None:
        row = self._conn.execute("SELECT status FROM github_deliveries WHERE delivery_key = ?", (delivery_key,)).fetchone()
        return row["status"] if row else None

    def pending(self, max_age_hours: int) -> list[tuple[str, str | None]]:
        """Livraisons acceptées mais jamais envoyées (bot arrêté entre-temps), de la plus ancienne à la plus récente."""
        rows = self._conn.execute(
            """
            SELECT delivery_key, notification FROM github_deliveries
            WHERE status = 'queued' AND received_at >= datetime('now', ?)
            ORDER BY received_at, rowid
            """,
            (f"-{int(max_age_hours)} hours",),
        ).fetchall()
        return [(row["delivery_key"], row["notification"]) for row in rows]

    def expire_queued(self, max_age_hours: int) -> int:
        """Abandonne les notifications trop anciennes pour être encore utiles (statut ``failed``)."""
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE github_deliveries SET status = 'failed', updated_at = datetime('now')
                WHERE status = 'queued' AND received_at < datetime('now', ?)
                """,
                (f"-{int(max_age_hours)} hours",),
            )
        return cursor.rowcount

    def last_received_at(self) -> str | None:
        row = self._conn.execute("SELECT MAX(received_at) AS last FROM github_deliveries").fetchone()
        return row["last"] if row else None

    def purge_older_than(self, days: int) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM github_deliveries WHERE received_at < datetime('now', ?)", (f"-{int(days)} days",)
            )
        return cursor.rowcount
