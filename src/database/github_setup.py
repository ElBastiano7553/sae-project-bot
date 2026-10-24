"""Connexion des projets GitHub (tables ``github_setup_states`` et ``github_installations``, v11).

* les jetons des liens d'installation et d'autorisation ne sont gardés que sous forme
  d'empreinte SHA-256, servent une seule fois (supprimés dès leur utilisation) et expirent ;
* une installation de la GitHub App n'est utilisable que par le serveur Discord pour lequel
  son accès a été vérifié auprès de GitHub : toutes les lectures prennent le ``guild_id``.

Aucun token GitHub n'est enregistré ici.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from src.database.connection import Database

_TIME = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME)


@dataclass(frozen=True)
class StateClaim:
    """Ce qu'un jeton valide autorise : ce serveur, cette personne, et pour l'autorisation, cette installation."""

    guild_id: int
    user_id: int
    installation_id: int | None = None


@dataclass(frozen=True)
class GithubInstallation:
    guild_id: int
    installation_id: int
    account: str
    repositories: tuple[str, ...]
    authorized_by: int
    authorized_at: str


class GithubSetupRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    # --- Jetons à usage unique ------------------------------------------------------------------

    def save_state(
        self, token_hash: str, step: str, guild_id: int, user_id: int, *, expires_at: datetime, installation_id: int | None = None
    ) -> None:
        with self._conn:
            self.purge_expired()
            self._conn.execute(
                "INSERT INTO github_setup_states (token_hash, step, guild_id, user_id, installation_id, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash, step, guild_id, user_id, installation_id, _fmt(expires_at)),
            )

    def consume_state(self, token_hash: str, step: str, *, now: datetime | None = None) -> StateClaim | None:
        """Utilise le jeton une seule fois : la ligne est supprimée, que la suite réussisse ou non.

        ``None`` s'il est inconnu, d'une autre étape, expiré ou déjà utilisé. Deux requêtes
        simultanées avec le même jeton : seule celle dont la suppression aboutit l'obtient
        (``DELETE`` conditionnel, atomique dans SQLite, même depuis deux connexions).
        """
        moment = _fmt(now or utcnow())
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM github_setup_states WHERE token_hash = ? AND step = ? AND expires_at > ?", (token_hash, step, moment)
            ).fetchone()
            if row is None:
                return None
            cursor = self._conn.execute(
                "DELETE FROM github_setup_states WHERE token_hash = ? AND step = ? AND expires_at > ?", (token_hash, step, moment)
            )
            if cursor.rowcount != 1:
                return None  # utilisé entre-temps par une autre requête
        return StateClaim(row["guild_id"], row["user_id"], row["installation_id"])

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Supprime les liens expirés jamais utilisés."""
        with self._conn:
            return self._conn.execute("DELETE FROM github_setup_states WHERE expires_at <= ?", (_fmt(now or utcnow()),)).rowcount

    def pending_states(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM github_setup_states").fetchone()[0]

    # --- Installations vérifiées, par serveur ----------------------------------------------------

    def save_installation(self, guild_id: int, installation_id: int, account: str, repositories, *, authorized_by: int) -> None:
        names = sorted({name for name in repositories}, key=str.casefold)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO github_installations (guild_id, installation_id, account, repositories, authorized_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, installation_id) DO UPDATE SET account = excluded.account,
                    repositories = excluded.repositories, authorized_by = excluded.authorized_by, authorized_at = datetime('now')
                """,
                (guild_id, installation_id, account, json.dumps(names), authorized_by),
            )

    def installations(self, guild_id: int) -> list[GithubInstallation]:
        rows = self._conn.execute(
            "SELECT * FROM github_installations WHERE guild_id = ? ORDER BY authorized_at DESC, installation_id", (guild_id,)
        )
        return [
            GithubInstallation(
                row["guild_id"], row["installation_id"], row["account"], tuple(json.loads(row["repositories"])),
                row["authorized_by"], row["authorized_at"],
            )
            for row in rows
        ]

    def installation_for(self, guild_id: int, repository: str) -> GithubInstallation | None:
        """Installation vérifiée pour CE serveur qui donne accès à ce dépôt (la plus récente), sinon None."""
        wanted = repository.casefold()
        for installation in self.installations(guild_id):
            if any(name.casefold() == wanted for name in installation.repositories):
                return installation
        return None
