"""Dépôt des réunions (Samae 2.0, lot 1) : seul endroit où l'on écrit le SQL des réunions.

Chaque modification passe par ``change`` : une transaction unique qui augmente la
révision de la réunion, vérifie au passage la révision attendue (bouton d'un panneau
périmé) et inscrit l'action dans ``meeting_log``. Si une étape échoue, rien n'est écrit.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from src.database.connection import Database


class RevisionConflict(Exception):
    """La réunion a changé depuis l'affichage du panneau (ou n'existe pas)."""


@dataclass(frozen=True)
class Meeting:
    id: int
    title: str
    description: str
    objectives: str
    leader_id: int
    leader_name: str
    status: str
    planned_minutes: int | None
    channel_id: int | None
    panel_message_id: int | None
    report: str | None
    report_channel_id: int | None
    report_message_ids: str | None
    revision: int
    created_by: int
    created_at: str
    started_at: str | None
    ended_at: str | None
    updated_at: str
    guild_id: int | None = None  # serveur Discord (None : réunion d'avant le multi-serveurs, pas encore rattachée)

    @property
    def report_messages(self) -> list[int]:
        try:
            return [int(i) for i in json.loads(self.report_message_ids or "[]")]
        except (TypeError, ValueError):
            return []


@dataclass(frozen=True)
class Participant:
    user_id: int
    user_name: str
    joined_at: str


@dataclass(frozen=True)
class Step:
    id: int
    position: int
    title: str
    status: str  # pending, current, done
    added_by: int
    created_at: str
    started_at: str | None
    done_at: str | None


@dataclass(frozen=True)
class Note:
    id: int
    kind: str  # decision, reserve, disagreement
    content: str
    rationale: str | None
    step_position: int | None
    author_id: int
    author_name: str
    created_at: str


@dataclass(frozen=True)
class ActionItem:
    id: int
    description: str
    assignee_id: int
    assignee_name: str
    due_date: str | None
    status: str
    created_by: int
    created_at: str


@dataclass(frozen=True)
class LogEntry:
    at: str
    user_id: int
    user_name: str
    action: str
    detail: str


@dataclass(frozen=True)
class MeetingDetail:
    """Une réunion et tout ce qui lui est rattaché, lu en une fois."""

    meeting: Meeting
    participants: list[Participant] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)

    @property
    def current_step(self) -> Step | None:
        return next((s for s in self.steps if s.status == "current"), None)

    def is_participant(self, user_id: int) -> bool:
        return any(p.user_id == user_id for p in self.participants)


_MEETING_COLUMNS = (
    "id, title, description, objectives, leader_id, leader_name, status, planned_minutes, channel_id, "
    "panel_message_id, report, report_channel_id, report_message_ids, revision, created_by, created_at, "
    "started_at, ended_at, updated_at, guild_id"
)


class MeetingChange:
    """Écritures possibles pendant une modification (toutes dans la même transaction)."""

    def __init__(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        self._conn = conn
        self.meeting_id = meeting_id

    def set_status(self, status: str, *, started: bool = False, ended: bool = False) -> None:
        self._conn.execute(
            f"""
            UPDATE meetings SET status = ?
                {", started_at = datetime('now')" if started else ""}
                {", ended_at = datetime('now')" if ended else ""}
             WHERE id = ?
            """,
            (status, self.meeting_id),
        )

    def set_step_status(self, position: int, status: str) -> None:
        stamps = {"current": ", started_at = datetime('now')", "done": ", done_at = datetime('now')"}.get(status, "")
        if status == "pending":
            stamps = ", started_at = NULL, done_at = NULL"
        cursor = self._conn.execute(
            f"UPDATE meeting_steps SET status = ? {stamps} WHERE meeting_id = ? AND position = ?",
            (status, self.meeting_id, position),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict(f"étape {position} introuvable")

    def add_step(self, title: str, added_by: int) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next FROM meeting_steps WHERE meeting_id = ?", (self.meeting_id,)
        ).fetchone()
        position = int(row["next"])
        self._conn.execute(
            "INSERT INTO meeting_steps (meeting_id, position, title, added_by) VALUES (?, ?, ?, ?)",
            (self.meeting_id, position, title, added_by),
        )
        return position

    def add_participant(self, user_id: int, user_name: str) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO meeting_participants (meeting_id, user_id, user_name) VALUES (?, ?, ?)",
            (self.meeting_id, user_id, user_name),
        )
        return cursor.rowcount == 1

    def remove_participant(self, user_id: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM meeting_participants WHERE meeting_id = ? AND user_id = ?", (self.meeting_id, user_id)
        )
        return cursor.rowcount == 1

    def add_note(self, kind: str, content: str, rationale: str | None, step_position: int | None, author_id: int, author_name: str) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO meeting_notes (meeting_id, kind, content, rationale, step_position, author_id, author_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (self.meeting_id, kind, content, rationale, step_position, author_id, author_name),
        )
        return int(cursor.lastrowid)

    def add_action(self, description: str, assignee_id: int, assignee_name: str, due_date: str | None, created_by: int) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO meeting_actions (meeting_id, description, assignee_id, assignee_name, due_date, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self.meeting_id, description, assignee_id, assignee_name, due_date, created_by),
        )
        return int(cursor.lastrowid)

    def set_report(self, report_json: str) -> None:
        self._conn.execute("UPDATE meetings SET report = ? WHERE id = ?", (report_json, self.meeting_id))


class MeetingRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    # --- Écritures ---------------------------------------------------------------

    def create(
        self,
        *,
        title: str,
        description: str,
        objectives: str,
        leader: tuple[int, str],
        participants: list[tuple[int, str]],
        steps: list[str],
        planned_minutes: int | None,
        channel_id: int | None,
        created_by: tuple[int, str],
        guild_id: int | None = None,
    ) -> int:
        """Crée la réunion, ses participants et son ordre du jour en une seule transaction."""
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO meetings (title, description, objectives, leader_id, leader_name, planned_minutes,
                                      channel_id, created_by, guild_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (title, description, objectives, leader[0], leader[1], planned_minutes, channel_id, created_by[0], guild_id),
            )
            meeting_id = int(cursor.lastrowid)
            change = MeetingChange(self._conn, meeting_id)
            for user_id, user_name in [leader, *participants]:
                change.add_participant(user_id, user_name)
            for title_step in steps:
                change.add_step(title_step, created_by[0])
            self._log(meeting_id, created_by, "création", title)
        return meeting_id

    @contextmanager
    def change(
        self, meeting_id: int, actor: tuple[int, str], action: str, detail: str = "", *, expected_revision: int | None = None
    ) -> Iterator[MeetingChange]:
        """Transaction de modification : révision vérifiée et augmentée, action journalisée.

        Lève ``RevisionConflict`` si la réunion n'existe pas ou a changé depuis ``expected_revision``.
        Toute exception levée dans le bloc annule l'ensemble des écritures.
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE meetings SET revision = revision + 1, updated_at = datetime('now')
                 WHERE id = ? AND (? IS NULL OR revision = ?)
                """,
                (meeting_id, expected_revision, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(f"réunion {meeting_id}")
            yield MeetingChange(self._conn, meeting_id)
            self._log(meeting_id, actor, action, detail)

    def set_panel(self, meeting_id: int, channel_id: int, message_id: int) -> None:
        """Emplacement du panneau (n'augmente pas la révision : le contenu n'a pas changé)."""
        with self._conn:
            self._conn.execute(
                "UPDATE meetings SET channel_id = ?, panel_message_id = ? WHERE id = ?", (channel_id, message_id, meeting_id)
            )

    def set_report_messages(self, meeting_id: int, channel_id: int, message_ids: list[int]) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE meetings SET report_channel_id = ?, report_message_ids = ? WHERE id = ?",
                (channel_id, json.dumps([int(i) for i in message_ids]), meeting_id),
            )

    def _log(self, meeting_id: int, actor: tuple[int, str], action: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO meeting_log (meeting_id, user_id, user_name, action, detail) VALUES (?, ?, ?, ?, ?)",
            (meeting_id, int(actor[0]), actor[1][:100], action[:50], detail[:300]),
        )

    # --- Lectures ----------------------------------------------------------------

    def get(self, meeting_id: int) -> Meeting | None:
        row = self._conn.execute(f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return Meeting(**dict(row)) if row else None

    def detail(self, meeting_id: int) -> MeetingDetail | None:
        meeting = self.get(meeting_id)
        if meeting is None:
            return None
        participants = [
            Participant(**dict(r))
            for r in self._conn.execute(
                "SELECT user_id, user_name, joined_at FROM meeting_participants WHERE meeting_id = ? ORDER BY joined_at, rowid",
                (meeting_id,),
            )
        ]
        steps = [
            Step(**dict(r))
            for r in self._conn.execute(
                "SELECT id, position, title, status, added_by, created_at, started_at, done_at "
                "FROM meeting_steps WHERE meeting_id = ? ORDER BY position",
                (meeting_id,),
            )
        ]
        notes = [
            Note(**dict(r))
            for r in self._conn.execute(
                "SELECT id, kind, content, rationale, step_position, author_id, author_name, created_at "
                "FROM meeting_notes WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            )
        ]
        actions = [
            ActionItem(**dict(r))
            for r in self._conn.execute(
                "SELECT id, description, assignee_id, assignee_name, due_date, status, created_by, created_at "
                "FROM meeting_actions WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            )
        ]
        return MeetingDetail(meeting, participants, steps, notes, actions)

    def list(
        self, statuses: tuple[str, ...] = (), limit: int = 25, search: str | None = None, *, guild_id: int | None = None
    ) -> list[Meeting]:
        """Réunions récentes ; avec ``guild_id``, seulement celles de ce serveur."""
        clauses, params = [], []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(int(guild_id))
        if statuses:
            clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
            params.extend(statuses)
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(title LIKE ? ESCAPE '\\' OR CAST(id AS TEXT) = ?)")
            params.extend([pattern, search.strip().lstrip("#")])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {_MEETING_COLUMNS} FROM meetings {where} ORDER BY id DESC LIMIT ?", (*params, int(limit))
        ).fetchall()
        return [Meeting(**dict(row)) for row in rows]

    def open_for(self, user_id: int, channel_id: int | None, *, guild_id: int | None = None) -> list[Meeting]:
        """Réunions prévues ou en cours de ce salon dont le membre fait partie (les plus récentes d'abord)."""
        rows = self._conn.execute(
            f"""
            SELECT {_MEETING_COLUMNS} FROM meetings
             WHERE status IN ('planned', 'running')
               AND (? IS NULL OR guild_id = ?)
               AND (channel_id = ? OR id IN (SELECT meeting_id FROM meeting_participants WHERE user_id = ?))
             ORDER BY (channel_id = ?) DESC, (status = 'running') DESC, id DESC
            """,
            (guild_id, guild_id, channel_id, user_id, channel_id),
        ).fetchall()
        return [Meeting(**dict(row)) for row in rows]

    def log(self, meeting_id: int, limit: int = 50) -> list[LogEntry]:
        rows = self._conn.execute(
            "SELECT at, user_id, user_name, action, detail FROM meeting_log WHERE meeting_id = ? ORDER BY id DESC LIMIT ?",
            (meeting_id, int(limit)),
        ).fetchall()
        return [LogEntry(**dict(row)) for row in rows]

    def count_created_since(self, user_id: int, since: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM meetings WHERE created_by = ? AND created_at >= ?", (int(user_id), since)
        ).fetchone()
        return int(row["n"])

    def adopt_orphans(self, guild_id: int) -> int:
        """Réunions créées avant le multi-serveurs (sans serveur) : rattachées au serveur principal. Retourne leur nombre."""
        with self._conn:
            cursor = self._conn.execute("UPDATE meetings SET guild_id = ? WHERE guild_id IS NULL", (int(guild_id),))
        return int(cursor.rowcount)
