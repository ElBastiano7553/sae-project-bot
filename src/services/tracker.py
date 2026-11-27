"""Suivi du projet (1.14.0) : le classeur « GAME DESIGN DATABASE » consulté et actualisé depuis Discord.

Source de vérité unique : **la base de Samae** après l'import du classeur (voir docs/suivi-excel.md).

* L'**import** (administrateur, aperçu puis confirmation) lit le classeur sans le modifier, garde une copie
  de l'original (``data/tracker/originals/<empreinte>.xlsx``, jamais réécrite), sauvegarde la base, puis
  ajoute ou met à jour les lignes. Rien n'est supprimé : une ligne absente du nouveau classeur reste.
* La **consultation** (recherche, fonctionnalités, décisions, idées, questions, progression) est libre.
* Les **modifications** passent par des propositions :
  - ajout d'une idée ou d'une question ouverte : appliqué directement (et historisé) ;
  - tout le reste (statut, ajout d'une décision, d'une fonctionnalité…) : **en attente** jusqu'à la
    validation d'un administrateur ; une décision officielle n'est jamais modifiée sans cette validation ;
  - une décision confirmée par un administrateur à l'issue d'un débat est enregistrée comme validée.
* L'**export** écrit une nouvelle copie du classeur (formules conservées), jamais l'original.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from src.database.connection import Database
from src.database.tracker import ChangeConflict, TrackerChange, TrackerRepository, TrackerRow
from src.services.backup import create_backup
from src.services.meetings import Actor, MeetingError, PermissionDenied
from src.tracker.workbook import (
    AUTHOR_HEADERS,
    DATE_HEADERS,
    DESCRIPTION_HEADERS,
    MAX_WORKBOOK_BYTES,
    ColumnInfo,
    ExportResult,
    SheetInfo,
    WorkbookInfo,
    export_workbook,
    file_hash,
    inspect_workbook,
    norm,
    read_rows,
)

FEATURES, IDEAS, DECISIONS, OPEN_QUESTIONS, CHANGELOG = "FEATURES", "IDEAS", "DECISIONS", "OPEN_QUESTIONS", "CHANGELOG"
# Ajouts appliqués sans validation (peu risqués, toujours historisés) ; tout le reste attend un administrateur.
DIRECT_ADD_SHEETS = (IDEAS, OPEN_QUESTIONS)
DONE_WORDS = ("termine", "fait", "done", "livre", "valide", "implemente", "fini", "complete", "ok", "100")
OPEN_WORDS = ("ouvert", "ouverte", "open", "enattente", "aetudier", "nonresolue", "atraiter", "todo", "afaire", "nouveau", "nouvelle")
TEXT_MAX = 500
SEARCH_LIMIT = 15
BACKUP_SUBDIR = "suivi"
BACKUP_KEEP = 30


class TrackerError(MeetingError):
    title = "Suivi du projet"


@dataclass(frozen=True)
class RowView:
    sheet: str
    key: str
    title: str
    status: str
    data: dict
    origin: str


def _info_from_dict(data: dict) -> WorkbookInfo:
    sheets = []
    for s in data["sheets"]:
        s = dict(s)
        s["columns"] = [ColumnInfo(**c) for c in s["columns"]]
        sheets.append(SheetInfo(**s))
    data = {**data, "sheets": sheets}
    return WorkbookInfo(**data)


def is_done(status: str) -> bool:
    key = norm(status)
    return bool(key) and any(key.startswith(w) or key == w for w in DONE_WORDS)


def is_open_question(status: str) -> bool:
    key = norm(status)
    return not key or any(key.startswith(w) for w in OPEN_WORDS)


class TrackerService:
    def __init__(self, db: Database, data_dir: Path, *, backup_dir: Path | None = None,
                 now=lambda: dt.datetime.now(dt.UTC)) -> None:
        self.db = db
        self.repo = TrackerRepository(db)
        self.data_dir = data_dir
        self.backup_dir = (backup_dir or data_dir.parent / "backups") / BACKUP_SUBDIR
        self._now = now

    # --- État ----------------------------------------------------------------------

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def info(self) -> WorkbookInfo | None:
        last = self.repo.last_import()
        if last is None:
            return None
        try:
            return _info_from_dict(last.report["workbook"])
        except (KeyError, TypeError):
            return None

    def require_info(self) -> WorkbookInfo:
        info = self.info()
        if info is None:
            raise TrackerError(
                "Le classeur de suivi n'a pas encore été importé : un administrateur doit lancer `/suivi importer` "
                "avec le fichier « GAME DESIGN DATABASE — LUNASTEH.xlsx »."
            )
        return info

    def sheet_info(self, sheet: str) -> SheetInfo | None:
        info = self.info()
        return info.sheet(sheet) if info else None

    def view(self, row: TrackerRow, sheet: SheetInfo | None = None) -> RowView:
        sheet = sheet or self.sheet_info(row.sheet)
        title = str(row.data.get(sheet.title_column) or "") if sheet and sheet.title_column else ""
        status = str(row.data.get(sheet.status_column) or "") if sheet and sheet.status_column else ""
        if not title:
            title = next((str(v) for v in row.data.values() if isinstance(v, str) and v.strip()), row.row_key)
        return RowView(row.sheet, row.row_key, title, status, row.data, row.origin)

    def rows(self, sheet: str) -> list[RowView]:
        info = self.sheet_info(sheet)
        return [self.view(r, info) for r in self.repo.rows(sheet)]

    # --- Import -----------------------------------------------------------------------

    def preview_import(self, data: bytes, filename: str) -> tuple[WorkbookInfo, dict[str, list], dict]:
        """Analyse sans rien écrire : retourne l'analyse, les lignes lues et les écarts avec Samae."""
        if len(data) > MAX_WORKBOOK_BYTES:
            raise TrackerError(f"Fichier trop gros (maximum {MAX_WORKBOOK_BYTES // 1024 // 1024} Mio).")
        try:
            info = inspect_workbook(data, filename)
            rows = read_rows(data, info)
        except TrackerError:
            raise
        except Exception as exc:  # fichier corrompu, mauvais format, mot de passe…
            raise TrackerError(f"Classeur illisible ({type(exc).__name__}) : enregistrez-le au format .xlsx puis réessayez.") from exc
        diff = {"new": 0, "changed": 0, "unchanged": 0, "missing": 0, "kept": 0}
        protected = self.repo.touched_since_import()
        for sheet, sheet_rows in rows.items():
            keys = set()
            for excel_row, key, record in sheet_rows:
                keys.add(key)
                current = self.repo.get_row(sheet, key)
                if (sheet, key) in protected:
                    diff["kept"] += 1
                elif current is None:
                    diff["new"] += 1
                elif current.data == record and current.excel_row == excel_row:
                    diff["unchanged"] += 1
                else:
                    diff["changed"] += 1
            diff["missing"] += len([k for k in self.repo.keys(sheet) if k not in keys and self.repo.get_row(sheet, k).origin == "excel"])
        diff["samae_rows"] = len([r for r in self.repo.all_rows() if r.origin == "samae"])
        return info, rows, diff

    def apply_import(self, data: bytes, filename: str, actor: Actor) -> dict:
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur importe le classeur de suivi.")
        info, rows, diff = self.preview_import(data, filename)
        self.backup("avant-import-suivi")
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        original = self.originals_dir / f"{info.sha256}.xlsx"
        if not original.exists():  # une copie par version du classeur, jamais réécrite
            original.write_bytes(data)
        counts = self.repo.apply_import(
            filename, info.sha256, actor.user_id, {"workbook": info.to_dict(), "diff": diff}, rows,
            protected=self.repo.touched_since_import(),
        )
        return {**counts, "missing": diff["missing"], "warnings": info.warnings}

    def backup(self, reason: str) -> Path | None:
        if self.db.in_memory:
            return None
        return create_backup(self.db.conn, self.backup_dir, keep=BACKUP_KEEP, reason=reason).path

    # --- Consultation ----------------------------------------------------------------

    def search(self, text: str, *, sheet: str | None = None, limit: int = SEARCH_LIMIT) -> list[RowView]:
        self.require_info()
        words = [w for w in re.findall(r"\w+", text.casefold()) if len(w) > 1]
        if not words:
            return []
        targets = [norm(w) for w in words]
        scored = []
        for row in self.repo.all_rows():
            if sheet and row.sheet != sheet:
                continue
            hay = norm(" ".join(str(v) for v in row.data.values() if v is not None)) + norm(row.row_key)
            hits = sum(1 for w in targets if w and w in hay)
            if hits:
                scored.append((hits, row))
        scored.sort(key=lambda s: -s[0])
        return [self.view(r) for _, r in scored[:limit]]

    def by_status(self, sheet: str, *, status: str | None = None, remaining: bool = False, open_only: bool = False) -> list[RowView]:
        self.require_info()
        rows = self.rows(sheet)
        if status:
            rows = [r for r in rows if norm(r.status) == norm(status)]
        if remaining:
            rows = [r for r in rows if not is_done(r.status)]
        if open_only:
            rows = [r for r in rows if is_open_question(r.status)]
        return rows

    def latest(self, sheet: str, limit: int = 10) -> list[RowView]:
        """Dernières lignes : d'abord celles ajoutées par Samae (plus récentes), puis les dernières du classeur."""
        rows = self.rows(sheet)
        samae = [r for r in rows if r.origin == "samae"][::-1]
        excel = [r for r in rows if r.origin == "excel"][::-1]
        return (samae + excel)[:limit]

    def progress(self) -> dict:
        self.require_info()
        features = self.rows(FEATURES)
        counts: dict[str, int] = {}
        for r in features:
            counts[r.status or "(sans statut)"] = counts.get(r.status or "(sans statut)", 0) + 1
        done = sum(1 for r in features if is_done(r.status))
        questions = [r for r in self.rows(OPEN_QUESTIONS) if is_open_question(r.status)]
        return {
            "features": len(features),
            "done": done,
            "percent": round(100 * done / len(features)) if features else 0,
            "by_status": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "ideas": len(self.rows(IDEAS)),
            "decisions": len(self.rows(DECISIONS)),
            "open_questions": len(questions),
            "pending_changes": len(self.repo.changes(status="pending", limit=500)),
        }

    def allowed_statuses(self, sheet: str) -> list[str]:
        info = self.sheet_info(sheet)
        col = info.column(info.status_column) if info else None
        if col is None:
            return []
        if col.validation and not col.validation[0].startswith("(liste"):
            return col.validation
        return col.values

    # --- Propositions ---------------------------------------------------------------

    def _column(self, sheet: SheetInfo, synonyms: tuple[str, ...]) -> str | None:
        wanted = [norm(s) for s in synonyms]
        for col in sheet.columns:
            if norm(col.header) in wanted and not col.formulas:
                return col.header
        return None

    def _new_key(self, sheet: SheetInfo) -> str:
        keys = self.repo.keys(sheet.name)
        if sheet.id_pattern and "#" in sheet.id_pattern:
            digits = sheet.id_pattern.count("#")
            prefix = sheet.id_pattern[: sheet.id_pattern.index("#")]
            numbers = [int(m.group(1)) for k in keys if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", k))]
            return f"{prefix}{(max(numbers, default=0) + 1):0{digits}d}"
        n = 1
        while f"SAMAE-{n}" in keys:
            n += 1
        return f"SAMAE-{n}"

    def propose_add(self, sheet_name: str, actor: Actor, *, title: str, description: str = "", status: str = "",
                    reason: str = "", validated_by: Actor | None = None) -> TrackerChange:
        info = self.require_info()
        sheet = info.sheet(sheet_name)
        if sheet is None or not sheet.columns:
            raise TrackerError(f"L'onglet {sheet_name} est absent du classeur importé (ou sans en-tête).")
        title = " ".join((title or "").split())[:TEXT_MAX]
        if not title:
            raise TrackerError("Le texte est obligatoire.")
        payload: dict = {}
        if sheet.title_column:
            payload[sheet.title_column] = title
        desc_col = self._column(sheet, DESCRIPTION_HEADERS)
        if description and desc_col and desc_col != sheet.title_column:
            payload[desc_col] = description.strip()[:TEXT_MAX]
        if sheet.status_column:
            allowed = self.allowed_statuses(sheet_name)
            if status and allowed and status not in allowed:
                raise TrackerError(f"Statut inconnu pour {sheet_name} : choisissez parmi {', '.join(allowed[:15])}.")
            if status:
                payload[sheet.status_column] = status
        date_col = self._column(sheet, DATE_HEADERS)
        if date_col:
            payload[date_col] = self._now().date().isoformat()
        author_col = self._column(sheet, AUTHOR_HEADERS)
        if author_col:
            payload[author_col] = actor.name
        if sheet.id_column:
            payload[sheet.id_column] = None  # attribué à l'application
        change = self.repo.propose("add", sheet_name, None, payload, reason[:300], actor.ref, None)
        if validated_by is not None:
            return self._apply(change, validated_by)
        if sheet_name in DIRECT_ADD_SHEETS:
            return self._apply(change, actor)
        return change

    def propose_update(self, sheet_name: str, key: str, actor: Actor, field: str, value: str, reason: str = "") -> TrackerChange:
        info = self.require_info()
        sheet = info.sheet(sheet_name)
        row = self.repo.get_row(sheet_name, key)
        if sheet is None or row is None:
            raise TrackerError(f"Ligne introuvable : {sheet_name} / {key}.")
        column = sheet.column(field)
        if column is None:
            raise TrackerError(f"Colonne inconnue dans {sheet_name} : {field}.")
        if column.formulas:
            raise TrackerError(f"La colonne « {field} » est calculée par une formule : elle n'est jamais modifiée par Samae.")
        if field == sheet.id_column:
            raise TrackerError("L'identifiant d'une ligne ne se modifie pas.")
        value = " ".join((value or "").split())[:TEXT_MAX]
        if field == sheet.status_column:
            allowed = self.allowed_statuses(sheet_name)
            if allowed and value not in allowed:
                raise TrackerError(f"Statut inconnu : choisissez parmi {', '.join(allowed[:15])}.")
        if row.data.get(field) == value:
            raise TrackerError("La valeur est déjà celle-ci : rien à proposer.")
        return self.repo.propose("update", sheet_name, key, {field: value}, reason[:300], actor.ref, {field: row.data.get(field)})

    def approve(self, change_id: int, actor: Actor) -> TrackerChange:
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur valide une modification du suivi.")
        change = self.repo.change(change_id)
        if change is None:
            raise TrackerError(f"Proposition #{change_id} introuvable.")
        return self._apply(change, actor)

    def _apply(self, change: TrackerChange, decider: Actor) -> TrackerChange:
        info = self.require_info()
        sheet = info.sheet(change.sheet)
        new_key = self._new_key(sheet) if change.kind == "add" and sheet is not None else None
        if change.kind == "add" and sheet is not None and sheet.id_column:
            change = TrackerChange(**{**change.__dict__, "payload": {**change.payload, sheet.id_column: new_key}})
            with self.db.conn:
                self.db.conn.execute("UPDATE tracker_changes SET payload = ? WHERE id = ?",
                                     (json.dumps(change.payload, ensure_ascii=False), change.id))
        self.backup("avant-modif-suivi")
        try:
            self.repo.apply(change, decider.ref, new_key)
        except ChangeConflict as exc:
            raise TrackerError(str(exc)) from None
        return self.repo.change(change.id)  # type: ignore[return-value]

    def reject(self, change_id: int, actor: Actor) -> None:
        change = self.repo.change(change_id)
        if change is None:
            raise TrackerError(f"Proposition #{change_id} introuvable.")
        if not actor.is_admin and change.proposed_by != actor.user_id:
            raise PermissionDenied("Seul un administrateur (ou l'auteur de la proposition) peut la refuser ou la retirer.")
        try:
            self.repo.reject(change_id, actor.ref)
        except ChangeConflict as exc:
            raise TrackerError(str(exc)) from None

    def pending(self) -> list[TrackerChange]:
        return self.repo.changes(status="pending", limit=25)

    def history(self, limit: int = 15) -> list[TrackerChange]:
        return [c for c in self.repo.changes(limit=limit * 2) if c.status != "pending"][:limit]

    # --- Export ----------------------------------------------------------------------

    def export(self, actor: Actor) -> ExportResult:
        if not actor.is_admin:
            raise PermissionDenied("Seul un administrateur exporte le classeur.")
        info = self.require_info()
        original = self.originals_dir / f"{info.sha256}.xlsx"
        if not original.is_file():
            raise TrackerError("La copie de l'original importé est introuvable sur le serveur : réimportez le classeur.")
        rows: dict[str, list] = {}
        for sheet in info.sheets:
            sheet_rows = self.repo.rows(sheet.name)
            if sheet_rows:
                rows[sheet.name] = [(r.excel_row, r.row_key, r.data) for r in sheet_rows]
        changelog = info.sheet(CHANGELOG)
        if changelog is not None and changelog.columns:
            rows.setdefault(CHANGELOG, [])
            rows[CHANGELOG] = rows[CHANGELOG] + self._changelog_rows(changelog)
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        out = self.exports_dir / f"{Path(info.filename).stem}-samae-{stamp}.xlsx"
        if out.resolve() == original.resolve():  # garde-fou : jamais l'original
            raise TrackerError("Chemin d'export invalide.")
        return export_workbook(original.read_bytes(), info, rows, out)

    def _changelog_rows(self, sheet: SheetInfo) -> list[tuple[None, str, dict]]:
        """Modifications appliquées par Samae, ajoutées au CHANGELOG de la copie exportée (pas en base)."""
        date_col = self._column(sheet, DATE_HEADERS)
        desc_col = self._column(sheet, DESCRIPTION_HEADERS) or sheet.title_column
        author_col = self._column(sheet, AUTHOR_HEADERS)
        if not desc_col:
            return []
        result = []
        for c in reversed(self.repo.changes(status="applied", limit=500)):
            if c.sheet == CHANGELOG:
                continue
            what = "ajout" if c.kind == "add" else "modification"
            detail = ", ".join(f"{k} = {v}" for k, v in c.payload.items() if v not in (None, ""))
            record = {desc_col: f"[Samae] {what} {c.sheet} {c.row_key or ''} : {detail}"[:500]}
            if date_col:
                record[date_col] = (c.decided_at or c.proposed_at)[:10]
            if author_col:
                record[author_col] = f"{c.proposer} (validé par {c.decider})" if c.decider != c.proposer else c.proposer
            result.append((None, f"samae-change-{c.id}", record))
        return result
