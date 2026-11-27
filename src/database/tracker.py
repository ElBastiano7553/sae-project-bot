"""Dépôt du suivi du projet (copie du classeur Excel, propositions de modification, historique)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.database.connection import Database


class ChangeConflict(Exception):
    """La proposition n'est plus en attente, ou la ligne a changé depuis la proposition."""


@dataclass(frozen=True)
class TrackerRow:
    sheet: str
    row_key: str
    excel_row: int | None
    data: dict
    origin: str
    updated_at: str


@dataclass(frozen=True)
class TrackerImport:
    id: int
    source_name: str
    source_hash: str
    imported_by: int
    imported_at: str
    report: dict


@dataclass(frozen=True)
class TrackerChange:
    id: int
    kind: str  # add, update
    sheet: str
    row_key: str | None
    payload: dict
    reason: str
    status: str  # pending, applied, rejected
    proposed_by: int
    proposer: str
    proposed_at: str
    decided_by: int | None
    decider: str | None
    decided_at: str | None
    before_data: dict | None


def _row(r) -> TrackerRow:
    return TrackerRow(r["sheet"], r["row_key"], r["excel_row"], json.loads(r["data"]), r["origin"], r["updated_at"])


def _change(r) -> TrackerChange:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    d["before_data"] = json.loads(d["before_data"]) if d["before_data"] else None
    return TrackerChange(**d)


_CHANGE_COLUMNS = (
    "id, kind, sheet, row_key, payload, reason, status, proposed_by, proposer, proposed_at, decided_by, decider, decided_at, before_data"
)


class TrackerRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    # --- Imports -----------------------------------------------------------------------

    def last_import(self) -> TrackerImport | None:
        r = self._conn.execute(
            "SELECT id, source_name, source_hash, imported_by, imported_at, report FROM tracker_imports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if r is None:
            return None
        return TrackerImport(r["id"], r["source_name"], r["source_hash"], r["imported_by"], r["imported_at"], json.loads(r["report"]))

    def touched_since_import(self) -> set[tuple[str, str]]:
        """Lignes modifiées par Samae (propositions appliquées) depuis le dernier import."""
        last = self.last_import()
        sql = "SELECT sheet, row_key FROM tracker_changes WHERE status = 'applied' AND row_key IS NOT NULL"
        params: tuple = ()
        if last is not None:
            sql += " AND decided_at >= ?"  # même seconde que l'import : comptée, par prudence
            params = (last.imported_at,)
        return {(r[0], r[1]) for r in self._conn.execute(sql, params).fetchall()}

    def apply_import(self, source_name: str, source_hash: str, imported_by: int, report: dict,
                     rows: dict[str, list[tuple[int, str, dict]]], *, protected: set[tuple[str, str]] = frozenset()) -> dict[str, int]:
        """Met à jour les lignes venues d'Excel. Rien n'est supprimé : une ligne absente du nouveau classeur
        reste dans Samae (et apparaît dans l'aperçu comme « absente du classeur »). Une ligne modifiée par
        Samae depuis le dernier import (``protected``) garde la version de Samae : la base est la référence."""
        counts = {"new": 0, "changed": 0, "unchanged": 0, "kept": 0}
        with self._conn:
            for sheet, sheet_rows in rows.items():
                for excel_row, key, data in sheet_rows:
                    if (sheet, key) in protected:
                        counts["kept"] += 1
                        self._conn.execute(
                            "UPDATE tracker_rows SET excel_row = ? WHERE sheet = ? AND row_key = ?", (excel_row, sheet, key)
                        )
                        continue
                    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
                    current = self._conn.execute(
                        "SELECT data, excel_row FROM tracker_rows WHERE sheet = ? AND row_key = ?", (sheet, key)
                    ).fetchone()
                    if current is None:
                        counts["new"] += 1
                    elif current["data"] == raw and current["excel_row"] == excel_row:
                        counts["unchanged"] += 1
                        continue
                    else:
                        counts["changed"] += 1
                    self._conn.execute(
                        """
                        INSERT INTO tracker_rows (sheet, row_key, excel_row, data, origin) VALUES (?, ?, ?, ?, 'excel')
                        ON CONFLICT(sheet, row_key) DO UPDATE SET excel_row = excluded.excel_row, data = excluded.data,
                            origin = 'excel', updated_at = datetime('now')
                        """,
                        (sheet, key, excel_row, raw),
                    )
            self._conn.execute(
                "INSERT INTO tracker_imports (source_name, source_hash, imported_by, report) VALUES (?, ?, ?, ?)",
                (source_name, source_hash, imported_by, json.dumps(report, ensure_ascii=False)),
            )
        return counts

    # --- Lignes ------------------------------------------------------------------------

    def rows(self, sheet: str) -> list[TrackerRow]:
        return [_row(r) for r in self._conn.execute(
            "SELECT sheet, row_key, excel_row, data, origin, updated_at FROM tracker_rows WHERE sheet = ? "
            "ORDER BY excel_row IS NULL, excel_row, updated_at", (sheet,)
        ).fetchall()]

    def all_rows(self) -> list[TrackerRow]:
        return [_row(r) for r in self._conn.execute(
            "SELECT sheet, row_key, excel_row, data, origin, updated_at FROM tracker_rows ORDER BY sheet, excel_row IS NULL, excel_row"
        ).fetchall()]

    def get_row(self, sheet: str, key: str) -> TrackerRow | None:
        r = self._conn.execute(
            "SELECT sheet, row_key, excel_row, data, origin, updated_at FROM tracker_rows WHERE sheet = ? AND row_key = ?",
            (sheet, key),
        ).fetchone()
        return _row(r) if r else None

    def keys(self, sheet: str) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT row_key FROM tracker_rows WHERE sheet = ?", (sheet,)).fetchall()]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tracker_rows").fetchone()[0])

    # --- Propositions ----------------------------------------------------------------

    def propose(self, kind: str, sheet: str, row_key: str | None, payload: dict, reason: str,
                proposer: tuple[int, str], before: dict | None) -> TrackerChange:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO tracker_changes (kind, sheet, row_key, payload, reason, proposed_by, proposer, before_data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, sheet, row_key, json.dumps(payload, ensure_ascii=False), reason, proposer[0], proposer[1],
                 json.dumps(before, ensure_ascii=False) if before is not None else None),
            )
        return self.change(int(cursor.lastrowid))  # type: ignore[return-value]

    def change(self, change_id: int) -> TrackerChange | None:
        r = self._conn.execute(f"SELECT {_CHANGE_COLUMNS} FROM tracker_changes WHERE id = ?", (change_id,)).fetchone()
        return _change(r) if r else None

    def changes(self, *, status: str | None = None, limit: int = 25) -> list[TrackerChange]:
        sql = f"SELECT {_CHANGE_COLUMNS} FROM tracker_changes"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_change(r) for r in self._conn.execute(sql, params).fetchall()]

    def apply(self, change: TrackerChange, decider: tuple[int, str], new_key: str | None) -> TrackerRow:
        """Applique une proposition EN ATTENTE, dans une seule transaction (ligne + statut de la proposition)."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE tracker_changes SET status = 'applied', decided_by = ?, decider = ?, decided_at = datetime('now'), "
                "row_key = COALESCE(row_key, ?) WHERE id = ? AND status = 'pending'",
                (decider[0], decider[1], new_key, change.id),
            )
            if cursor.rowcount == 0:
                raise ChangeConflict("Cette proposition n'est plus en attente.")
            if change.kind == "add":
                key = new_key or change.row_key
                self._conn.execute(
                    "INSERT INTO tracker_rows (sheet, row_key, excel_row, data, origin) VALUES (?, ?, NULL, ?, 'samae')",
                    (change.sheet, key, json.dumps(change.payload, ensure_ascii=False, sort_keys=True)),
                )
            else:
                key = change.row_key
                current = self._conn.execute(
                    "SELECT data FROM tracker_rows WHERE sheet = ? AND row_key = ?", (change.sheet, key)
                ).fetchone()
                if current is None:
                    raise ChangeConflict("La ligne visée n'existe plus.")
                data = json.loads(current["data"])
                for field, value in (change.before_data or {}).items():
                    if data.get(field) != value:
                        raise ChangeConflict(
                            f"La ligne a changé depuis la proposition (« {field} » vaut maintenant « {data.get(field)} ») : "
                            "refaites la proposition."
                        )
                data.update(change.payload)
                self._conn.execute(
                    "UPDATE tracker_rows SET data = ?, updated_at = datetime('now') WHERE sheet = ? AND row_key = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), change.sheet, key),
                )
        return self.get_row(change.sheet, key)  # type: ignore[arg-type,return-value]

    def reject(self, change_id: int, decider: tuple[int, str]) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE tracker_changes SET status = 'rejected', decided_by = ?, decider = ?, decided_at = datetime('now') "
                "WHERE id = ? AND status = 'pending'",
                (decider[0], decider[1], change_id),
            )
            if cursor.rowcount == 0:
                raise ChangeConflict("Cette proposition n'est plus en attente.")
