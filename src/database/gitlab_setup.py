"""Connexion des projets GitLab (table ``gitlab_setup_states``, v12).

Les jetons des liens d'autorisation ne sont gardés que sous forme d'empreinte SHA-256, servent une
seule fois (supprimés dès leur utilisation) et expirent. Chaque jeton est lié à un serveur Discord,
une personne et un projet GitLab de ce serveur : il ne peut pas servir à relier un autre projet.

Aucun token GitLab n'est enregistré ici.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.database.connection import Database
from src.database.github_setup import _fmt, utcnow


@dataclass(frozen=True)
class GitlabStateClaim:
    """Ce qu'un jeton valide autorise : ce serveur, cette personne, ce projet."""

    guild_id: int
    user_id: int
    project_id: int


class GitlabSetupRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def save_state(self, token_hash: str, step: str, guild_id: int, user_id: int, *, expires_at: datetime, project_id: int) -> None:
        """``step`` : toujours « gitlab » (même interface que ``GithubSetupRepository`` pour ``SetupStates``)."""
        with self._conn:
            self.purge_expired()
            self._conn.execute(
                "INSERT INTO gitlab_setup_states (token_hash, guild_id, user_id, project_id, expires_at) VALUES (?, ?, ?, ?, ?)",
                (token_hash, guild_id, user_id, project_id, _fmt(expires_at)),
            )

    def consume_state(self, token_hash: str, step: str, *, now: datetime | None = None) -> GitlabStateClaim | None:
        """Utilise le jeton une seule fois : la ligne est supprimée, que la suite réussisse ou non.

        ``None`` s'il est inconnu, expiré ou déjà utilisé ; avec deux requêtes simultanées, seule celle
        dont la suppression aboutit l'obtient (comme ``GithubSetupRepository.consume_state``).
        """
        moment = _fmt(now or utcnow())
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM gitlab_setup_states WHERE token_hash = ? AND expires_at > ?", (token_hash, moment)
            ).fetchone()
            if row is None:
                return None
            cursor = self._conn.execute("DELETE FROM gitlab_setup_states WHERE token_hash = ? AND expires_at > ?", (token_hash, moment))
            if cursor.rowcount != 1:
                return None
        return GitlabStateClaim(row["guild_id"], row["user_id"], row["project_id"])

    def purge_expired(self, *, now: datetime | None = None) -> int:
        with self._conn:
            return self._conn.execute("DELETE FROM gitlab_setup_states WHERE expires_at <= ?", (_fmt(now or utcnow()),)).rowcount

    def pending_states(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM gitlab_setup_states").fetchone()[0]
