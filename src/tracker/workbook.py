"""Lecture et copie du classeur « GAME DESIGN DATABASE » (openpyxl), sans jamais modifier l'original.

* ``inspect_workbook`` analyse un classeur : onglets, ligne d'en-tête, colonnes, colonnes à formules,
  identifiants, statuts (valeurs observées et listes de validation), graphiques et images présents ;
* ``read_rows`` extrait les lignes de données (une ligne Excel = un dictionnaire « en-tête → valeur ») ;
* ``export_workbook`` écrit une **nouvelle** copie : l'original sert de modèle, seules les cellules de
  données modifiées ou ajoutées sont écrites, les cellules à formule ne sont jamais écrasées et les
  formules des nouvelles lignes sont recopiées de la ligne du dessus (références décalées).

Limite connue d'openpyxl : les graphiques et images d'un classeur ne sont pas conservés dans la copie.
``inspect_workbook`` le signale (``drawings``) et l'export l'indique : l'original, lui, reste intact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXPECTED_SHEETS = (
    "DASHBOARD", "FEATURES", "IDEAS", "DECISIONS", "GAMEPLAY", "UI_UX", "ART_DIRECTION", "OPEN_QUESTIONS", "CHANGELOG",
)
# Synonymes d'en-têtes (comparés sans accents, majuscules, espaces ni ponctuation).
ID_HEADERS = ("id", "identifiant", "ref", "reference", "code", "numero", "num", "no", "n")
TITLE_HEADERS = (
    "titre", "title", "nom", "name", "intitule", "fonctionnalite", "feature", "idee", "idea", "decision", "question",
    "element", "sujet", "libelle", "resume", "description courte",
)
STATUS_HEADERS = ("statut", "status", "etat", "state", "avancement", "progression")
DATE_HEADERS = ("date", "jour", "le", "datemodification", "datedecision", "creele", "dateajout")
DESCRIPTION_HEADERS = ("description", "detail", "details", "modification", "changement", "commentaire", "notes", "contenu")
AUTHOR_HEADERS = ("auteur", "author", "par", "proprietaire", "owner", "responsable", "porteur")
HEADER_SCAN_ROWS = 15
MAX_ROWS_PER_SHEET = 5000
MAX_WORKBOOK_BYTES = 10 * 1024 * 1024


def norm(text: object) -> str:
    plain = unicodedata.normalize("NFKD", str(text or "").casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", plain)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


@dataclass
class ColumnInfo:
    letter: str
    index: int  # 1 = colonne A
    header: str
    filled: int = 0
    formulas: int = 0
    sample_formula: str = ""
    distinct: int = 0
    validation: list[str] = field(default_factory=list)  # liste déroulante (validation de données)
    values: list[str] = field(default_factory=list)  # valeurs observées (colonne de statut seulement)


@dataclass
class SheetInfo:
    name: str
    header_row: int | None
    data_rows: int
    max_row: int
    max_column: int
    columns: list[ColumnInfo]
    id_column: str | None
    title_column: str | None
    status_column: str | None
    formula_cells: int
    merged_ranges: int
    tables: list[str]
    duplicate_ids: list[str] = field(default_factory=list)
    id_pattern: str = ""
    cross_references: dict[str, int] = field(default_factory=dict)  # autre onglet → nb de formules qui le citent

    def column(self, header: str | None) -> ColumnInfo | None:
        return next((c for c in self.columns if c.header == header), None) if header else None

    @property
    def formula_headers(self) -> list[str]:
        return [c.header for c in self.columns if c.formulas]


@dataclass
class WorkbookInfo:
    filename: str
    sha256: str
    size: int
    sheets: list[SheetInfo]
    missing_sheets: list[str]
    extra_sheets: list[str]
    drawings: list[str]  # graphiques et images (perdus par openpyxl dans une copie)
    defined_names: list[str]
    warnings: list[str]

    def sheet(self, name: str) -> SheetInfo | None:
        return next((s for s in self.sheets if s.name == name), None)

    def to_dict(self) -> dict:
        return asdict(self)


def _load(data: bytes, *, data_only: bool = False):
    from openpyxl import load_workbook

    return load_workbook(io.BytesIO(data), data_only=data_only, keep_links=True)


def _pick(headers: list[str], synonyms: tuple[str, ...]) -> str | None:
    normalized = {norm(h): h for h in headers if h}
    for synonym in synonyms:
        if norm(synonym) in normalized:
            return normalized[norm(synonym)]
    for synonym in synonyms:  # en-tête qui commence par le synonyme (« Statut actuel », « ID feature »)
        for key, header in normalized.items():
            if len(norm(synonym)) >= 2 and key.startswith(norm(synonym)):
                return header
    return None


def _header_row(ws) -> int | None:
    best, best_count = None, 1
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row or 1, HEADER_SCAN_ROWS)):
        count = sum(1 for c in row if isinstance(c.value, str) and c.value.strip() and not c.value.startswith("="))
        if count > best_count:
            best, best_count = row[0].row, count
    return best


def _headers(ws, header_row: int) -> list[tuple[int, str]]:
    result, seen = [], set()
    for cell in ws[header_row]:
        if cell.value is None or not str(cell.value).strip():
            continue
        header = " ".join(str(cell.value).split())
        base, n = header, 2
        while header in seen:  # en-têtes en double : « Notes », « Notes (2) »
            header, n = f"{base} ({n})", n + 1
        seen.add(header)
        result.append((cell.column, header))
    return result


def _validations(ws) -> dict[int, list[str]]:
    """Listes déroulantes (validation de données « liste ») par numéro de colonne."""
    from openpyxl.utils import range_boundaries

    result: dict[int, list[str]] = {}
    for dv in getattr(ws.data_validations, "dataValidation", []) or []:
        if dv.type != "list" or not dv.formula1:
            continue
        formula = dv.formula1.strip()
        if formula.startswith('"') and formula.endswith('"'):
            values = [v.strip() for v in formula[1:-1].split(",") if v.strip()]
        else:
            values = [f"(liste définie par {formula})"]
        for cell_range in str(dv.sqref).split():
            try:
                min_col, _, max_col, _ = range_boundaries(cell_range)
            except ValueError:
                continue
            for col in range(min_col or 1, (max_col or min_col or 1) + 1):
                result.setdefault(col, values)
    return result


def _drawings(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return []
    found = []
    charts = [n for n in names if n.startswith("xl/charts/chart")]
    images = [n for n in names if n.startswith("xl/media/")]
    pivots = [n for n in names if n.startswith("xl/pivotTables/")]
    if charts:
        found.append(f"{len(charts)} graphique(s)")
    if images:
        found.append(f"{len(images)} image(s)")
    if pivots:
        found.append(f"{len(pivots)} tableau(x) croisé(s) dynamique(s)")
    return found


def _id_pattern(ids: list[str]) -> str:
    """Motif des identifiants (« F-001 » → « F-### ») si tous les identifiants le suivent."""
    patterns = {re.sub(r"\d", "#", i) for i in ids if i}
    return patterns.pop() if len(patterns) == 1 else ""


def inspect_workbook(data: bytes, filename: str = "classeur.xlsx") -> WorkbookInfo:
    if len(data) > MAX_WORKBOOK_BYTES:
        raise ValueError(f"Classeur trop gros ({len(data) // 1024} Kio, maximum {MAX_WORKBOOK_BYTES // 1024 // 1024} Mio).")
    from openpyxl.utils import get_column_letter

    wb = _load(data)
    sheets: list[SheetInfo] = []
    warnings: list[str] = []
    sheet_names = list(wb.sheetnames)
    for ws in wb.worksheets:
        header_row = _header_row(ws)
        columns: list[ColumnInfo] = []
        formula_cells = 0
        refs: dict[str, int] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    formula_cells += 1
                    text = str(cell.value)
                    for other in sheet_names:
                        if other != ws.title and (f"{other}!" in text or f"'{other}'!" in text):
                            refs[other] = refs.get(other, 0) + 1
        data_rows = 0
        id_col = title_col = status_col = None
        duplicates: list[str] = []
        pattern = ""
        if header_row:
            validations = _validations(ws)
            headers = _headers(ws, header_row)
            by_index = {idx: ColumnInfo(get_column_letter(idx), idx, header, validation=validations.get(idx, [])) for idx, header in headers}
            distinct: dict[int, set] = {idx: set() for idx in by_index}
            for row in ws.iter_rows(min_row=header_row + 1, max_row=min(ws.max_row, header_row + MAX_ROWS_PER_SHEET)):
                filled_row = False
                for cell in row:
                    info = by_index.get(cell.column)
                    if info is None or cell.value is None or (isinstance(cell.value, str) and not cell.value.strip()):
                        continue
                    filled_row = True
                    info.filled += 1
                    if cell.data_type == "f":
                        info.formulas += 1
                        info.sample_formula = info.sample_formula or str(cell.value)[:120]
                    else:
                        distinct[cell.column].add(str(cell.value).strip())
                if filled_row:
                    data_rows += 1
            for idx, info in by_index.items():
                info.distinct = len(distinct[idx])
            columns = list(by_index.values())
            names = [c.header for c in columns]
            id_col = _pick(names, ID_HEADERS)
            status_col = _pick(names, STATUS_HEADERS)
            title_col = _pick([n for n in names if n not in (id_col, status_col)], TITLE_HEADERS)
            if title_col is None:
                title_col = next((c.header for c in columns if c.header not in (id_col, status_col) and not c.formulas), None)
            status_info = next((c for c in columns if c.header == status_col), None)
            if status_info is not None:
                status_info.values = sorted(distinct[status_info.index])[:40]
            id_info = next((c for c in columns if c.header == id_col), None)
            if id_info is not None:
                values = [str(c.value).strip() for c in ws[id_info.letter][header_row:] if c.value not in (None, "")]
                seen: set[str] = set()
                duplicates = sorted({v for v in values if v in seen or seen.add(v)})
                pattern = _id_pattern(values)
                if duplicates:
                    warnings.append(f"{ws.title} : identifiants en double ({', '.join(duplicates[:5])}).")
        elif ws.title != "DASHBOARD":
            warnings.append(f"{ws.title} : aucune ligne d'en-tête reconnue.")
        tables = list(getattr(ws, "tables", {}).keys())
        sheets.append(
            SheetInfo(
                ws.title, header_row, data_rows, ws.max_row, ws.max_column, columns, id_col, title_col, status_col,
                formula_cells, len(ws.merged_cells.ranges), tables, duplicates, pattern, refs,
            )
        )
    missing = [s for s in EXPECTED_SHEETS if s not in sheet_names]
    extra = [s for s in sheet_names if s not in EXPECTED_SHEETS]
    if missing:
        warnings.append(f"Onglets attendus absents : {', '.join(missing)}.")
    drawings = _drawings(data)
    if drawings:
        warnings.append(
            f"Le classeur contient {', '.join(drawings)} : openpyxl ne les recopie pas dans un export. "
            "L'original n'est jamais modifié ; gardez-le comme référence pour le DASHBOARD."
        )
    defined = list(getattr(wb, "defined_names", {}).keys()) if hasattr(wb.defined_names, "keys") else []
    return WorkbookInfo(filename, file_hash(data), len(data), sheets, missing, extra, drawings, defined, warnings)


def read_rows(data: bytes, info: WorkbookInfo) -> dict[str, list[tuple[int, str, dict]]]:
    """Lignes de données de chaque onglet : [(ligne Excel, clé, {en-tête: valeur})].

    Pour les colonnes à formule, la valeur enregistrée est le **résultat** mis en cache par Excel
    (le classeur doit avoir été enregistré par Excel ou LibreOffice) ; la formule reste dans l'original.
    """
    values_wb = _load(data, data_only=True)
    result: dict[str, list[tuple[int, str, dict]]] = {}
    for sheet in info.sheets:
        if not sheet.header_row or not sheet.columns:
            continue
        ws = values_wb[sheet.name]
        rows: list[tuple[int, str, dict]] = []
        used: set[str] = set()
        id_info = sheet.column(sheet.id_column)
        for row in ws.iter_rows(min_row=sheet.header_row + 1, max_row=min(ws.max_row, sheet.header_row + MAX_ROWS_PER_SHEET)):
            cells = {c.column: c.value for c in row}
            record = {col.header: _json_value(cells.get(col.index)) for col in sheet.columns}
            if all(v is None or (isinstance(v, str) and not v.strip()) for v in record.values()):
                continue
            excel_row = row[0].row
            key = str(record.get(id_info.header) or "").strip() if id_info else ""
            if not key or key in used:
                key = f"ligne-{excel_row}"
            used.add(key)
            rows.append((excel_row, key, record))
        result[sheet.name] = rows
    return result


@dataclass
class ExportResult:
    path: Path
    written_cells: int
    appended_rows: int
    skipped_formula_cells: int
    warnings: list[str]


def export_workbook(template: bytes, info: WorkbookInfo, rows: dict[str, list[tuple[int | None, str, dict]]], out_path: Path) -> ExportResult:
    """Écrit une copie à jour à ``out_path`` (jamais au chemin de l'original). ``rows`` : état actuel de Samae."""
    from openpyxl.formula.translate import Translator

    wb = _load(template)
    written = appended = skipped = 0
    warnings: list[str] = list(info.warnings[-1:]) if info.drawings else []
    for sheet in info.sheets:
        if sheet.name not in rows or not sheet.header_row:
            continue
        ws = wb[sheet.name]
        by_header = {c.header: c for c in sheet.columns}
        last_row = max([sheet.header_row] + [r for r, _, _ in rows[sheet.name] if r])
        for excel_row, _key, record in rows[sheet.name]:
            target = excel_row
            if target is None:
                last_row += 1
                target = last_row
                appended += 1
                for col in sheet.columns:  # formules de la ligne du dessus, recopiées et décalées
                    above = ws.cell(row=target - 1, column=col.index)
                    if above.data_type == "f" and target - 1 > sheet.header_row:
                        ws.cell(row=target, column=col.index).value = Translator(str(above.value), origin=above.coordinate).translate_formula(
                            ws.cell(row=target, column=col.index).coordinate
                        )
            for header, value in record.items():
                col = by_header.get(header)
                if col is None:
                    continue
                cell = ws.cell(row=target, column=col.index)
                if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value.startswith("=")):
                    skipped += 1  # jamais de valeur écrite à la place d'une formule
                    continue
                current = _json_value(cell.value)
                if current == value or (current in (None, "") and value in (None, "")):
                    continue
                cell.value = value
                written += 1
    try:
        wb.calculation.fullCalcOnLoad = True  # Excel recalcule le DASHBOARD à l'ouverture
    except AttributeError:
        pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return ExportResult(out_path, written, appended, skipped, warnings)


def report_markdown(info: WorkbookInfo) -> str:
    """Rapport lisible de l'analyse (utilisé par ``python -m src.tracker.inspect``)."""
    lines = [
        f"# Analyse du classeur « {info.filename} »",
        "",
        f"- Taille : {info.size // 1024} Kio · empreinte SHA-256 : `{info.sha256[:16]}…`",
        f"- Onglets : {', '.join(s.name for s in info.sheets)}",
        f"- Onglets attendus absents : {', '.join(info.missing_sheets) or 'aucun'}",
        f"- Onglets supplémentaires : {', '.join(info.extra_sheets) or 'aucun'}",
        f"- Graphiques, images, tableaux croisés : {', '.join(info.drawings) or 'aucun'}",
        f"- Noms définis : {', '.join(info.defined_names) or 'aucun'}",
        "",
    ]
    for warning in info.warnings:
        lines.append(f"> ⚠️ {warning}")
    for s in info.sheets:
        lines += [
            "",
            f"## {s.name}",
            "",
            f"- Ligne d'en-tête : {s.header_row or 'non trouvée'} · lignes de données : {s.data_rows} · "
            f"cellules à formule : {s.formula_cells} · plages fusionnées : {s.merged_ranges}"
            + (f" · tableaux Excel : {', '.join(s.tables)}" if s.tables else ""),
            f"- Identifiant : {s.id_column or 'aucun (clé « ligne-N »)'}"
            + (f" (motif {s.id_pattern})" if s.id_pattern else "")
            + f" · titre : {s.title_column or '?'} · statut : {s.status_column or 'aucun'}",
        ]
        if s.cross_references:
            lines.append("- Formules qui citent d'autres onglets : " + ", ".join(f"{k} ({v})" for k, v in s.cross_references.items()))
        if s.columns:
            lines += ["", "| Col. | En-tête | Remplies | Formules | Valeurs distinctes | Liste déroulante |", "|---|---|---|---|---|---|"]
            for c in s.columns:
                validation = ", ".join(c.validation)[:80]
                lines.append(
                    f"| {c.letter} | {c.header} | {c.filled} | {c.formulas}{f' (`{c.sample_formula}`)' if c.sample_formula else ''} "
                    f"| {c.distinct} | {validation} |"
                )
            status = s.column(s.status_column)
            if status and status.values:
                lines.append(f"\nStatuts observés : {', '.join(status.values)}")
    return "\n".join(lines) + "\n"
