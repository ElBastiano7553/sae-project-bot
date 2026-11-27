"""Dépôts du gestionnaire de réunions (1.14.0) : idées, tier lists et débats.

Seul endroit où l'on écrit le SQL de ces tables (migration v15). Les vérifications qui
doivent tenir même en cas de clics simultanés (plafond de cinq idées actives, vote sur
une tier list ouverte, bulletin non validé) sont faites **dans la transaction**
d'écriture, avec des ``UPDATE … WHERE`` / ``INSERT … SELECT`` conditionnels.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.database.connection import Database

TIERS = ("S", "A", "B", "C", "D", "F")


class LimitReached(Exception):
    """Plafond atteint (vérifié dans la transaction)."""


class StateConflict(Exception):
    """L'objet n'est plus dans l'état attendu (fermé, validé, supprimé…)."""


# --- Idées --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Idea:
    id: int
    guild_id: int | None
    author_id: int
    author_name: str
    name: str
    play_style: str
    genre: str
    description: str
    status: str  # active, retained, rejected, withdrawn
    group_id: int | None
    meeting_id: int | None
    created_at: str
    updated_at: str


_IDEA_COLUMNS = (
    "id, guild_id, author_id, author_name, name, play_style, genre, description, status, group_id, meeting_id, "
    "created_at, updated_at"
)


class IdeaRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, idea_id: int) -> Idea | None:
        row = self._conn.execute(f"SELECT {_IDEA_COLUMNS} FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        return Idea(**dict(row)) if row else None

    def list(
        self, guild_id: int | None, *, statuses: tuple[str, ...] = (), author_id: int | None = None, limit: int = 200
    ) -> list[Idea]:
        sql = f"SELECT {_IDEA_COLUMNS} FROM ideas WHERE guild_id IS ?"
        params: list = [guild_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            params += list(statuses)
        if author_id is not None:
            sql += " AND author_id = ?"
            params.append(author_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [Idea(**dict(r)) for r in self._conn.execute(sql, params).fetchall()]

    def count_active(self, guild_id: int | None, author_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE guild_id IS ? AND author_id = ? AND status = 'active'", (guild_id, author_id)
        ).fetchone()
        return int(row[0])

    def create(
        self,
        guild_id: int | None,
        author: tuple[int, str],
        *,
        name: str,
        play_style: str,
        genre: str,
        description: str,
        meeting_id: int | None,
        max_active: int,
    ) -> Idea:
        """Crée l'idée seulement si l'auteur a moins de ``max_active`` idées actives (dans la même transaction)."""
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO ideas (guild_id, author_id, author_name, name, play_style, genre, description, meeting_id)
                SELECT ?, ?, ?, ?, ?, ?, ?, ?
                WHERE (SELECT COUNT(*) FROM ideas WHERE guild_id IS ? AND author_id = ? AND status = 'active') < ?
                """,
                (guild_id, author[0], author[1], name, play_style, genre, description, meeting_id, guild_id, author[0], max_active),
            )
            if cursor.rowcount == 0:
                raise LimitReached()
            idea_id = int(cursor.lastrowid)
            self._log(idea_id, author, "proposition", name)
        return self.get(idea_id)  # type: ignore[return-value]

    def update(self, idea_id: int, actor: tuple[int, str], fields: dict[str, str], *, expected_status: tuple[str, ...]) -> Idea:
        allowed = {"name", "play_style", "genre", "description"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return self.get(idea_id)  # type: ignore[return-value]
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE ideas SET {sets}, updated_at = datetime('now') "
                f"WHERE id = ? AND status IN ({', '.join('?' * len(expected_status))})",
                (*fields.values(), idea_id, *expected_status),
            )
            if cursor.rowcount == 0:
                raise StateConflict()
            self._log(idea_id, actor, "modification", ", ".join(sorted(fields)))
        return self.get(idea_id)  # type: ignore[return-value]

    def set_status(self, idea_id: int, actor: tuple[int, str], status: str, *, detail: str = "", max_active: int | None = None) -> Idea:
        """Change le statut ; réactiver une idée vérifie à nouveau le plafond de l'auteur dans la transaction."""
        with self._conn:
            if status == "active" and max_active is not None:
                cursor = self._conn.execute(
                    """
                    UPDATE ideas SET status = 'active', updated_at = datetime('now')
                    WHERE id = ? AND status != 'active'
                      AND (SELECT COUNT(*) FROM ideas o WHERE o.guild_id IS ideas.guild_id
                           AND o.author_id = ideas.author_id AND o.status = 'active') < ?
                    """,
                    (idea_id, max_active),
                )
                if cursor.rowcount == 0:
                    raise LimitReached()
            else:
                cursor = self._conn.execute(
                    "UPDATE ideas SET status = ?, updated_at = datetime('now') WHERE id = ?", (status, idea_id)
                )
                if cursor.rowcount == 0:
                    raise StateConflict()
            self._log(idea_id, actor, f"statut:{status}", detail)
        return self.get(idea_id)  # type: ignore[return-value]

    def set_group(self, idea_ids: list[int], group_id: int | None, actor: tuple[int, str]) -> None:
        with self._conn:
            for idea_id in idea_ids:
                self._conn.execute(
                    "UPDATE ideas SET group_id = ?, updated_at = datetime('now') WHERE id = ?",
                    (None if group_id == idea_id else group_id, idea_id),
                )
                self._log(idea_id, actor, "regroupement", f"groupe #{group_id}" if group_id else "retiré du groupe")

    def history(self, idea_id: int, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT at, user_name, action, detail FROM idea_history WHERE idea_id = ? ORDER BY id DESC LIMIT ?",
            (idea_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _log(self, idea_id: int, actor: tuple[int, str], action: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO idea_history (idea_id, user_id, user_name, action, detail) VALUES (?, ?, ?, ?, ?)",
            (idea_id, actor[0], actor[1], action, detail[:300]),
        )


# --- Tier lists ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierList:
    id: int
    guild_id: int | None
    title: str
    meeting_id: int | None
    status: str  # open, closed, cancelled
    created_by: int
    created_at: str
    closed_at: str | None
    results: str | None

    @property
    def results_data(self) -> dict | None:
        try:
            return json.loads(self.results) if self.results else None
        except ValueError:
            return None


_TIERLIST_COLUMNS = "id, guild_id, title, meeting_id, status, created_by, created_at, closed_at, results"


@dataclass(frozen=True)
class Voter:
    voter_id: int
    voter_name: str
    validated_at: str | None


class TierListRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, tierlist_id: int) -> TierList | None:
        row = self._conn.execute(f"SELECT {_TIERLIST_COLUMNS} FROM tierlists WHERE id = ?", (tierlist_id,)).fetchone()
        return TierList(**dict(row)) if row else None

    def list(self, guild_id: int | None, *, statuses: tuple[str, ...] = (), limit: int = 25, meeting_id: int | None = None) -> list[TierList]:
        sql = f"SELECT {_TIERLIST_COLUMNS} FROM tierlists WHERE guild_id IS ?"
        params: list = [guild_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            params += list(statuses)
        if meeting_id is not None:
            sql += " AND meeting_id = ?"
            params.append(meeting_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [TierList(**dict(r)) for r in self._conn.execute(sql, params).fetchall()]

    def create(self, guild_id: int | None, title: str, idea_ids: list[int], created_by: int, meeting_id: int | None) -> TierList:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO tierlists (guild_id, title, meeting_id, created_by) VALUES (?, ?, ?, ?)",
                (guild_id, title, meeting_id, created_by),
            )
            tierlist_id = int(cursor.lastrowid)
            self._conn.executemany(
                "INSERT INTO tierlist_items (tierlist_id, idea_id, position) VALUES (?, ?, ?)",
                [(tierlist_id, idea_id, pos) for pos, idea_id in enumerate(idea_ids, 1)],
            )
        return self.get(tierlist_id)  # type: ignore[return-value]

    def item_ids(self, tierlist_id: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT idea_id FROM tierlist_items WHERE tierlist_id = ? ORDER BY position", (tierlist_id,)
        ).fetchall()
        return [int(r[0]) for r in rows]

    def voter(self, tierlist_id: int, voter_id: int) -> Voter | None:
        row = self._conn.execute(
            "SELECT voter_id, voter_name, validated_at FROM tierlist_voters WHERE tierlist_id = ? AND voter_id = ?",
            (tierlist_id, voter_id),
        ).fetchone()
        return Voter(**dict(row)) if row else None

    def voters(self, tierlist_id: int) -> list[Voter]:
        rows = self._conn.execute(
            "SELECT voter_id, voter_name, validated_at FROM tierlist_voters WHERE tierlist_id = ? ORDER BY voter_name",
            (tierlist_id,),
        ).fetchall()
        return [Voter(**dict(r)) for r in rows]

    def ballot(self, tierlist_id: int, voter_id: int) -> dict[int, str]:
        rows = self._conn.execute(
            "SELECT idea_id, tier FROM tierlist_votes WHERE tierlist_id = ? AND voter_id = ?", (tierlist_id, voter_id)
        ).fetchall()
        return {int(r[0]): r[1] for r in rows}

    def all_votes(self, tierlist_id: int, *, validated_only: bool = True) -> dict[int, list[tuple[int, str]]]:
        """Votes par idée : [(votant, rang)]. Lu seulement à la clôture (votes masqués avant)."""
        sql = "SELECT v.idea_id, v.voter_id, v.tier FROM tierlist_votes v"
        if validated_only:
            sql += (
                " JOIN tierlist_voters p ON p.tierlist_id = v.tierlist_id AND p.voter_id = v.voter_id"
                " AND p.validated_at IS NOT NULL"
            )
        sql += " WHERE v.tierlist_id = ? ORDER BY v.idea_id, v.voter_id"
        result: dict[int, list[tuple[int, str]]] = {}
        for row in self._conn.execute(sql, (tierlist_id,)).fetchall():
            result.setdefault(int(row[0]), []).append((int(row[1]), row[2]))
        return result

    def vote(self, tierlist_id: int, voter: tuple[int, str], idea_id: int, tier: str) -> None:
        """Enregistre (ou remplace) un vote, seulement si la tier list est ouverte et le bulletin non validé."""
        if tier not in TIERS:
            raise ValueError(tier)
        with self._conn:
            state = self._conn.execute(
                """
                SELECT t.status, p.validated_at,
                       EXISTS (SELECT 1 FROM tierlist_items i WHERE i.tierlist_id = t.id AND i.idea_id = ?) AS listed
                FROM tierlists t
                LEFT JOIN tierlist_voters p ON p.tierlist_id = t.id AND p.voter_id = ?
                WHERE t.id = ?
                """,
                (idea_id, voter[0], tierlist_id),
            ).fetchone()
            if state is None or state["status"] != "open" or state["validated_at"] is not None or not state["listed"]:
                raise StateConflict()
            self._conn.execute(
                "INSERT INTO tierlist_voters (tierlist_id, voter_id, voter_name) VALUES (?, ?, ?) "
                "ON CONFLICT(tierlist_id, voter_id) DO UPDATE SET voter_name = excluded.voter_name",
                (tierlist_id, voter[0], voter[1]),
            )
            self._conn.execute(
                "INSERT INTO tierlist_votes (tierlist_id, idea_id, voter_id, tier) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tierlist_id, idea_id, voter_id) DO UPDATE SET tier = excluded.tier, updated_at = datetime('now')",
                (tierlist_id, idea_id, voter[0], tier),
            )

    def validate(self, tierlist_id: int, voter_id: int) -> None:
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE tierlist_voters SET validated_at = datetime('now')
                WHERE tierlist_id = ? AND voter_id = ? AND validated_at IS NULL
                  AND (SELECT status FROM tierlists WHERE id = ?) = 'open'
                """,
                (tierlist_id, voter_id, tierlist_id),
            )
            if cursor.rowcount == 0:
                raise StateConflict()

    def close(self, tierlist_id: int, results: dict, *, status: str = "closed") -> TierList:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE tierlists SET status = ?, closed_at = datetime('now'), results = ? WHERE id = ? AND status = 'open'",
                (status, json.dumps(results, ensure_ascii=False), tierlist_id),
            )
            if cursor.rowcount == 0:
                raise StateConflict()
        return self.get(tierlist_id)  # type: ignore[return-value]


# --- Débats -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Debate:
    id: int
    guild_id: int | None
    title: str
    idea_id: int | None
    tierlist_id: int | None
    meeting_id: int | None
    status: str  # open, voting, proposed, decided, cancelled
    created_by: int
    created_at: str
    proposal: str | None
    proposal_idea_status: str | None
    proposed_by: int | None
    decision: str | None
    decided_by: int | None
    decided_at: str | None


_DEBATE_COLUMNS = (
    "id, guild_id, title, idea_id, tierlist_id, meeting_id, status, created_by, created_at, proposal, "
    "proposal_idea_status, proposed_by, decision, decided_by, decided_at"
)


@dataclass(frozen=True)
class DebateEntry:
    id: int
    kind: str  # pour, contre, objection
    content: str
    author_id: int
    author_name: str
    created_at: str


@dataclass(frozen=True)
class DebateOption:
    id: int
    label: str
    votes: int = 0


class DebateRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, debate_id: int) -> Debate | None:
        row = self._conn.execute(f"SELECT {_DEBATE_COLUMNS} FROM debates WHERE id = ?", (debate_id,)).fetchone()
        return Debate(**dict(row)) if row else None

    def list(self, guild_id: int | None, *, statuses: tuple[str, ...] = (), meeting_id: int | None = None, limit: int = 25) -> list[Debate]:
        sql = f"SELECT {_DEBATE_COLUMNS} FROM debates WHERE guild_id IS ?"
        params: list = [guild_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            params += list(statuses)
        if meeting_id is not None:
            sql += " AND meeting_id = ?"
            params.append(meeting_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [Debate(**dict(r)) for r in self._conn.execute(sql, params).fetchall()]

    def find_open_for_idea(self, guild_id: int | None, idea_id: int) -> Debate | None:
        row = self._conn.execute(
            f"SELECT {_DEBATE_COLUMNS} FROM debates WHERE guild_id IS ? AND idea_id = ? "
            "AND status IN ('open', 'voting', 'proposed') ORDER BY id DESC LIMIT 1",
            (guild_id, idea_id),
        ).fetchone()
        return Debate(**dict(row)) if row else None

    def create(
        self, guild_id: int | None, title: str, created_by: int, *, idea_id: int | None, tierlist_id: int | None, meeting_id: int | None
    ) -> Debate:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO debates (guild_id, title, idea_id, tierlist_id, meeting_id, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, title, idea_id, tierlist_id, meeting_id, created_by),
            )
        return self.get(int(cursor.lastrowid))  # type: ignore[return-value]

    def entries(self, debate_id: int) -> list[DebateEntry]:
        rows = self._conn.execute(
            "SELECT id, kind, content, author_id, author_name, created_at FROM debate_entries WHERE debate_id = ? ORDER BY id",
            (debate_id,),
        ).fetchall()
        return [DebateEntry(**dict(r)) for r in rows]

    def add_entry(self, debate_id: int, kind: str, content: str, author: tuple[int, str], *, max_entries: int) -> None:
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO debate_entries (debate_id, kind, content, author_id, author_name)
                SELECT ?, ?, ?, ?, ?
                WHERE (SELECT status FROM debates WHERE id = ?) IN ('open', 'voting', 'proposed')
                  AND (SELECT COUNT(*) FROM debate_entries WHERE debate_id = ?) < ?
                """,
                (debate_id, kind, content, author[0], author[1], debate_id, debate_id, max_entries),
            )
            if cursor.rowcount == 0:
                raise StateConflict()

    def options(self, debate_id: int) -> list[DebateOption]:
        rows = self._conn.execute(
            """
            SELECT o.id, o.label, COUNT(v.voter_id) AS votes
            FROM debate_options o LEFT JOIN debate_votes v ON v.option_id = o.id
            WHERE o.debate_id = ? GROUP BY o.id ORDER BY o.id
            """,
            (debate_id,),
        ).fetchall()
        return [DebateOption(**dict(r)) for r in rows]

    def voter_count(self, debate_id: int) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM debate_votes WHERE debate_id = ?", (debate_id,)).fetchone()[0])

    def my_vote(self, debate_id: int, voter_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT option_id FROM debate_votes WHERE debate_id = ? AND voter_id = ?", (debate_id, voter_id)
        ).fetchone()
        return int(row[0]) if row else None

    def start_vote(self, debate_id: int, labels: list[str]) -> None:
        with self._conn:
            cursor = self._conn.execute("UPDATE debates SET status = 'voting' WHERE id = ? AND status = 'open'", (debate_id,))
            if cursor.rowcount == 0:
                raise StateConflict()
            self._conn.executemany(
                "INSERT INTO debate_options (debate_id, label) VALUES (?, ?)", [(debate_id, label) for label in labels]
            )

    def vote(self, debate_id: int, voter_id: int, option_id: int) -> None:
        with self._conn:
            ok = self._conn.execute(
                "SELECT 1 FROM debates d JOIN debate_options o ON o.debate_id = d.id "
                "WHERE d.id = ? AND o.id = ? AND d.status = 'voting'",
                (debate_id, option_id),
            ).fetchone()
            if ok is None:
                raise StateConflict()
            self._conn.execute(
                "INSERT INTO debate_votes (debate_id, voter_id, option_id) VALUES (?, ?, ?) "
                "ON CONFLICT(debate_id, voter_id) DO UPDATE SET option_id = excluded.option_id, voted_at = datetime('now')",
                (debate_id, voter_id, option_id),
            )

    def propose(self, debate_id: int, proposal: str, idea_status: str | None, proposed_by: int) -> Debate:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE debates SET status = 'proposed', proposal = ?, proposal_idea_status = ?, proposed_by = ? "
                "WHERE id = ? AND status IN ('open', 'voting', 'proposed')",
                (proposal, idea_status, proposed_by, debate_id),
            )
            if cursor.rowcount == 0:
                raise StateConflict()
        return self.get(debate_id)  # type: ignore[return-value]

    def decide(self, debate_id: int, decided_by: int, expected_proposal: str) -> Debate:
        """Confirme la proposition en cours ; refusé si elle a changé entre l'affichage et le clic."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE debates SET status = 'decided', decision = proposal, decided_by = ?, decided_at = datetime('now') "
                "WHERE id = ? AND status = 'proposed' AND proposal = ?",
                (decided_by, debate_id, expected_proposal),
            )
            if cursor.rowcount == 0:
                raise StateConflict()
        return self.get(debate_id)  # type: ignore[return-value]

    def cancel(self, debate_id: int) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE debates SET status = 'cancelled' WHERE id = ? AND status IN ('open', 'voting', 'proposed')", (debate_id,)
            )
            if cursor.rowcount == 0:
                raise StateConflict()
