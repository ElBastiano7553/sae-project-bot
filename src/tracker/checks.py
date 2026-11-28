"""Vérification du classeur « GAME DESIGN DATABASE » : cohérence des onglets, sans rien modifier.

Le classeur est lu tel qu'il s'affiche (une liste de lignes de textes par onglet) : il peut venir
d'un fichier Excel (``sheets_from_xlsx``) ou du Google Sheet de l'équipe exporté en .xlsx
(``src/tracker/google.py``). ``verify_workbook`` renvoie un ``Report`` : une liste de constats
classés par gravité, chacun situé (onglet, identifiant, cellule) et expliqué en français.

Ce qui est vérifié (codes stables, utilisés dans les tests et la documentation) :

* structure : onglets attendus, ligne d'en-tête, colonnes attendues (``onglet``, ``entete``, ``colonne``) ;
* identifiants : présents, uniques, au bon format, numérotation continue (``id-*``) ;
* listes : statuts, catégories, priorités, domaines, types conformes aux listes déroulantes (``liste``) ;
* champs obligatoires remplis (``obligatoire``) ;
* dates : format JJ/MM/AAAA, pas dans le futur, validation après l'ajout (``date-*``) ;
* renvois : tout identifiant cité (« voir Q-004 », « FEAT-001 à FEAT-008 ») existe (``renvoi``) ;
* règles de l'équipe : un élément VALIDÉ, EN COURS ou TERMINÉ a une décision dans DECISIONS
  (``decision-absente``) ; décisions remplacées et questions résolues complètes (``decision-*``,
  ``question-*``) ;
* DASHBOARD : chaque compteur affiché est recalculé à partir des onglets (``dashboard``) ;
* CHANGELOG : chaque élément y apparaît au moins une fois (``changelog-absent``).

Aucune règle ne suppose un contenu : seules la forme et la cohérence sont contrôlées.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from src.tracker.workbook import EXPECTED_SHEETS, MAX_WORKBOOK_BYTES, norm

Sheets = Mapping[str, Sequence[Sequence[str]]]

# --- Listes de référence (celles du Google Sheet de l'équipe, novembre 2026) ------------------------------

ITEM_STATUSES = ("PROPOSÉ", "À DISCUTER", "À CLARIFIER", "VALIDÉ", "EN COURS", "TERMINÉ", "ABANDONNÉ")
IDEA_STATUSES = ("PROPOSÉ", "À DISCUTER", "À CLARIFIER", "VALIDÉ", "ABANDONNÉ")
DECISION_STATES = ("ACTIVE", "REMPLACÉE", "ANNULÉE")
QUESTION_STATUSES = ("OUVERTE", "EN COURS", "RÉSOLUE", "ABANDONNÉE")
CATEGORIES = (
    "Gameplay", "Progression", "Équilibrage", "Économie", "Narration et univers", "Interface (UI/UX)", "Art et animation",
    "Audio", "Multijoueur et social", "Technique", "Monétisation", "Autre",
)
PRIORITIES = ("Haute", "Moyenne", "Basse")
GAMEPLAY_DOMAINS = (
    "Mode de jeu", "Système de jeu", "Interaction", "Progression", "Équilibrage", "Condition de victoire",
    "Condition de défaite", "Récompense", "Pénalité", "Autre",
)
UI_TYPES = ("Écran", "Menu", "Navigation", "Interface", "Animation", "Interaction utilisateur", "Élément d'interface", "Autre")
ART_TYPES = ("Palette de couleurs", "Typographie", "Ambiance", "Référence visuelle", "Inspiration", "Élément graphique validé", "Autre")
CHANGE_TYPES = ("Ajout", "Complément", "Modification", "Décision", "Remplacement", "Abandon", "Correction", "Structure")
# Statuts qui supposent une décision de l'équipe (DASHBOARD : « statuts VALIDÉ, EN COURS et TERMINÉ »).
DECIDED_STATUSES = ("VALIDÉ", "EN COURS", "TERMINÉ")


@dataclass(frozen=True)
class SheetSpec:
    name: str
    prefix: str | None  # None : préfixe libre, mais le même pour tout l'onglet
    id_column: str | None
    title_column: str | None
    status_column: str | None
    required: tuple[str, ...] = ()
    lists: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    dates: tuple[str, ...] = ()
    added_date: str | None = None
    validated_date: str | None = None
    decided_statuses: tuple[str, ...] = ()  # statuts qui demandent une décision dans DECISIONS


SPECS: tuple[SheetSpec, ...] = (
    SheetSpec(
        "FEATURES", "FEAT", "ID", "Nom de la fonctionnalité", "Statut",
        required=("Nom de la fonctionnalité", "Catégorie", "Statut", "Date d'ajout"),
        lists={"Catégorie": CATEGORIES, "Priorité": PRIORITIES, "Statut": ITEM_STATUSES},
        dates=("Date d'ajout", "Date de validation"), added_date="Date d'ajout", validated_date="Date de validation",
        decided_statuses=DECIDED_STATUSES,
    ),
    SheetSpec(
        "IDEAS", "IDEA", "ID", "Nom de l'idée", "Statut",
        required=("Nom de l'idée", "Statut", "Date d'ajout"),
        lists={"Catégorie": CATEGORIES, "Statut": IDEA_STATUSES},
        dates=("Date d'ajout",), added_date="Date d'ajout", decided_statuses=("VALIDÉ",),
    ),
    SheetSpec(
        "DECISIONS", "DEC", "ID", "Sujet", "État de la décision",
        required=("Sujet", "Décision prise", "Date", "État de la décision"),
        lists={"État de la décision": DECISION_STATES}, dates=("Date",), added_date="Date",
    ),
    SheetSpec(
        "GAMEPLAY", "GP", "ID", "Nom", "Statut",
        required=("Domaine", "Nom", "Statut", "Date d'ajout"),
        lists={"Domaine": GAMEPLAY_DOMAINS, "Statut": ITEM_STATUSES},
        dates=("Date d'ajout",), added_date="Date d'ajout", decided_statuses=DECIDED_STATUSES,
    ),
    SheetSpec(
        "UI_UX", None, "ID", "Nom", "Statut",
        required=("Type d'élément", "Nom", "Statut", "Date d'ajout"),
        lists={"Type d'élément": UI_TYPES, "Statut": ITEM_STATUSES},
        dates=("Date d'ajout",), added_date="Date d'ajout", decided_statuses=DECIDED_STATUSES,
    ),
    SheetSpec(
        "ART_DIRECTION", "ART", "ID", "Nom", "Statut",
        required=("Type", "Nom", "Statut", "Date d'ajout"),
        lists={"Type": ART_TYPES, "Statut": ITEM_STATUSES},
        dates=("Date d'ajout",), added_date="Date d'ajout", decided_statuses=DECIDED_STATUSES,
    ),
    SheetSpec(
        "OPEN_QUESTIONS", "Q", "ID", "Question", "Statut",
        required=("Question", "Date", "Statut"),
        lists={"Statut": QUESTION_STATUSES}, dates=("Date",), added_date="Date",
    ),
    SheetSpec(
        "CHANGELOG", None, None, "Élément concerné", None,
        required=("Date", "Type de modification", "Élément concerné", "Origine de la modification"),
        lists={"Type de modification": CHANGE_TYPES}, dates=("Date",),
    ),
)
SPEC_BY_NAME = {s.name: s for s in SPECS}
KNOWN_PREFIXES = {s.prefix for s in SPECS if s.prefix}
# Colonnes dont le contenu est un identifiant (un renvoi cassé y est une erreur, pas un avertissement).
ID_FIELDS = ("Remplace (ID décision)", "Décision liée (ID)", "Fonctionnalités concernées")

_ID = re.compile(r"\b([A-Z]{1,6})-(\d{3,4})\b")
_RANGE = re.compile(r"\b([A-Z]{1,6})-(\d{3,4})\s*(?:à|a|au|-|–|—)\s*(?:\1-)?(\d{3,4})\b")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
MAX_RANGE = 200


class Level(StrEnum):
    ERROR = "erreur"
    WARNING = "avertissement"
    INFO = "information"


LEVEL_ORDER = {Level.ERROR: 0, Level.WARNING: 1, Level.INFO: 2}
LEVEL_ICON = {Level.ERROR: "❌", Level.WARNING: "⚠️", Level.INFO: "ℹ️"}


@dataclass(frozen=True)
class Finding:
    level: Level
    code: str
    sheet: str
    message: str
    item: str = ""  # identifiant concerné (FEAT-002…), s'il y en a un
    cell: str = ""  # cellule (I4), si elle est connue

    def where(self) -> str:
        parts = [self.sheet] + [p for p in (self.item, self.cell) if p]
        return " · ".join(parts)


@dataclass
class Report:
    source: str
    checked_at: str
    findings: list[Finding]
    counts: dict[str, int]  # lignes lues par onglet

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.INFO]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        rows = sum(self.counts.values())
        verdict = "aucune erreur" if self.ok else f"{len(self.errors)} erreur(s)"
        return f"{rows} ligne(s) vérifiée(s) dans {len(self.counts)} onglet(s) : {verdict}, {len(self.warnings)} avertissement(s), {len(self.infos)} information(s)."


# --- Lecture ---------------------------------------------------------------------------------------------


def cell_text(value: object) -> str:
    """Texte d'une cellule tel que l'équipe le lit (dates au format JJ/MM/AAAA)."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%d/%m/%Y") if value.time() == dt.time() else value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, dt.date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def sheets_from_xlsx(data: bytes) -> dict[str, list[list[str]]]:
    """Valeurs affichées de chaque onglet d'un .xlsx (résultats des formules mis en cache, jamais les formules)."""
    if len(data) > MAX_WORKBOOK_BYTES:
        raise ValueError(f"Classeur trop gros ({len(data) // 1024} Kio, maximum {MAX_WORKBOOK_BYTES // 1024 // 1024} Mio).")
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        result: dict[str, list[list[str]]] = {}
        for ws in wb.worksheets:
            rows = [[cell_text(v) for v in row] for row in ws.iter_rows(values_only=True, max_row=5000)]
            while rows and not any(rows[-1]):
                rows.pop()
            result[ws.title] = rows
        return result
    finally:
        wb.close()


def column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


@dataclass
class _Row:
    number: int  # ligne du tableur (1 = première ligne)
    values: dict[str, str]  # en-tête (tel qu'écrit) → texte
    letters: dict[str, str]  # en-tête → lettre de colonne

    def get(self, header: str | None) -> str:
        return self.values.get(header, "") if header else ""

    def cell(self, header: str | None) -> str:
        return f"{self.letters[header]}{self.number}" if header and header in self.letters else f"ligne {self.number}"


@dataclass
class _Table:
    name: str
    header_row: int | None
    headers: list[str]
    rows: list[_Row]
    spec: SheetSpec | None
    by_id: dict[str, _Row] = field(default_factory=dict)

    def header(self, wanted: str | None) -> str | None:
        """En-tête réel correspondant à ``wanted`` (comparé sans accents ni ponctuation)."""
        if wanted is None:
            return None
        target = norm(wanted)
        return next((h for h in self.headers if norm(h) == target), None)


def _find_header_row(rows: Sequence[Sequence[str]], spec: SheetSpec | None) -> int | None:
    expected = {norm(c) for c in _spec_columns(spec)} if spec else set()
    for index, row in enumerate(rows[:15]):
        cells = {norm(c) for c in row if c}
        if not cells:
            continue
        if expected and len(cells & expected) >= max(2, len(expected) // 2):
            return index
        if not expected and "id" in cells and len(cells) >= 3:
            return index
    return None


def _spec_columns(spec: SheetSpec) -> list[str]:
    columns = [spec.id_column, spec.title_column, spec.status_column, *spec.required, *spec.lists, *spec.dates]
    return list(dict.fromkeys(c for c in columns if c))


def _table(name: str, rows: Sequence[Sequence[str]]) -> _Table:
    spec = SPEC_BY_NAME.get(name)
    header_index = _find_header_row(rows, spec)
    if header_index is None:
        return _Table(name, None, [], [], spec)
    raw_headers = list(rows[header_index])
    headers = [h for h in raw_headers if h]
    table_rows: list[_Row] = []
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        values: dict[str, str] = {}
        letters: dict[str, str] = {}
        for col, header in enumerate(raw_headers):
            if not header:
                continue
            values[header] = row[col].strip() if col < len(row) and isinstance(row[col], str) else cell_text(row[col] if col < len(row) else None)
            letters[header] = column_letter(col)
        if any(values.values()):
            table_rows.append(_Row(offset, values, letters))
    return _Table(name, header_index + 1, headers, table_rows, spec)


# --- Outils ----------------------------------------------------------------------------------------------


def parse_date(text: str) -> dt.date | None:
    match = _DATE.match(text.strip())
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    try:
        return dt.date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def mentioned_ids(text: str, prefixes: Iterable[str] = KNOWN_PREFIXES) -> list[str]:
    """Identifiants cités dans un texte, plages comprises (« FEAT-001 à FEAT-003 » → 3 identifiants)."""
    allowed = set(prefixes)
    found: list[str] = []
    for match in _RANGE.finditer(text):
        prefix, start, end = match.group(1), match.group(2), match.group(3)
        if prefix not in allowed:
            continue
        low, high = int(start), int(end)
        if low <= high <= low + MAX_RANGE:
            found.extend(f"{prefix}-{n:0{len(start)}d}" for n in range(low, high + 1))
    for match in _ID.finditer(text):
        if match.group(1) in allowed:
            found.append(match.group(0))
    return list(dict.fromkeys(found))


def _same(a: str, b: str) -> bool:
    return norm(a) == norm(b)


# --- Vérification ----------------------------------------------------------------------------------------


class _Checker:
    def __init__(self, sheets: Sheets, *, today: dt.date) -> None:
        self.today = today
        self.findings: list[Finding] = []
        self.tables: dict[str, _Table] = {name: _table(name, rows) for name, rows in sheets.items()}
        self.raw = sheets
        self.prefixes = set(KNOWN_PREFIXES)

    def add(self, level: Level, code: str, sheet: str, message: str, *, item: str = "", cell: str = "") -> None:
        self.findings.append(Finding(level, code, sheet, message, item, cell))

    def run(self) -> list[Finding]:
        self.structure()
        for table in self.tables.values():
            if table.spec is not None and table.header_row is not None:
                self.identifiers(table)
        for table in self.tables.values():
            if table.spec is not None and table.header_row is not None:
                self.lists_and_fields(table)
        self.references()
        self.decisions()
        self.questions()
        self.changelog()
        self.dashboard()
        return sorted(self.findings, key=lambda f: (LEVEL_ORDER[f.level], EXPECTED_SHEETS.index(f.sheet) if f.sheet in EXPECTED_SHEETS else 99))

    # Structure

    def structure(self) -> None:
        for name in EXPECTED_SHEETS:
            if name not in self.tables:
                self.add(Level.ERROR, "onglet", name, "Onglet absent du classeur.")
        for name, table in self.tables.items():
            if name not in EXPECTED_SHEETS:
                self.add(Level.INFO, "onglet", name, "Onglet non prévu : il n'est pas vérifié.")
                continue
            if table.spec is None:
                continue
            if table.header_row is None:
                if any(any(c for c in row) for row in self.raw[name]):
                    self.add(Level.ERROR, "entete", name, "Ligne d'en-tête introuvable (colonnes attendues : "
                             + ", ".join(_spec_columns(table.spec)[:5]) + "…).")
                continue
            for column in _spec_columns(table.spec):
                if table.header(column) is None:
                    self.add(Level.WARNING, "colonne", name, f"Colonne « {column} » introuvable (renommée ou supprimée ?) : "
                             "les vérifications qui l'utilisent sont ignorées.")

    # Identifiants

    def identifiers(self, table: _Table) -> None:
        spec = table.spec
        id_header = table.header(spec.id_column)
        if id_header is None:
            return
        numbers: dict[str, list[int]] = {}
        for row in table.rows:
            key = row.get(id_header)
            if not key:
                self.add(Level.ERROR, "id-manquant", table.name, "Ligne remplie sans identifiant.", cell=row.cell(id_header))
                continue
            if key in table.by_id:
                self.add(Level.ERROR, "id-double", table.name, f"Identifiant en double (lignes {table.by_id[key].number} et {row.number}).",
                         item=key, cell=row.cell(id_header))
                continue
            table.by_id[key] = row
            match = re.fullmatch(r"([A-Z]{1,6})-(\d{3,4})", key)
            if match is None or (spec.prefix and match.group(1) != spec.prefix):
                expected = f"{spec.prefix}-001" if spec.prefix else "XX-001"
                self.add(Level.ERROR, "id-format", table.name, f"Identifiant mal formé (attendu sur le modèle {expected}).",
                         item=key, cell=row.cell(id_header))
                continue
            numbers.setdefault(match.group(1), []).append(int(match.group(2)))
        if spec.prefix is None and len(numbers) > 1:
            self.add(Level.WARNING, "id-format", table.name, "Plusieurs préfixes d'identifiants dans le même onglet : "
                     + ", ".join(sorted(numbers)) + ".")
        for prefix, values in numbers.items():
            self.prefixes.add(prefix)
            missing = sorted(set(range(1, max(values) + 1)) - set(values))
            if missing:
                shown = ", ".join(f"{prefix}-{n:03d}" for n in missing[:10])
                self.add(Level.INFO, "id-trou", table.name, f"Numérotation non continue : {shown} absent(s) (supprimé(s) ?).")

    # Listes, champs obligatoires, dates

    def lists_and_fields(self, table: _Table) -> None:
        spec = table.spec
        id_header = table.header(spec.id_column)
        for row in table.rows:
            item = row.get(id_header)
            for column in spec.required:
                header = table.header(column)
                if header is not None and not row.get(header):
                    self.add(Level.WARNING, "obligatoire", table.name, f"« {column} » n'est pas rempli.", item=item, cell=row.cell(header))
            for column, allowed in spec.lists.items():
                header = table.header(column)
                value = row.get(header)
                if header is None or not value:
                    continue
                if not any(_same(value, a) for a in allowed):
                    self.add(Level.ERROR, "liste", table.name, f"« {column} » vaut « {value} », absent de la liste ({', '.join(allowed)}).",
                             item=item, cell=row.cell(header))
                elif value not in allowed:
                    exact = next(a for a in allowed if _same(value, a))
                    self.add(Level.WARNING, "liste", table.name, f"« {column} » vaut « {value} » : écrire exactement « {exact} » "
                             "(sinon les compteurs du DASHBOARD l'ignorent).", item=item, cell=row.cell(header))
            parsed: dict[str, dt.date] = {}
            for column in spec.dates:
                header = table.header(column)
                value = row.get(header)
                if header is None or not value:
                    continue
                date = parse_date(value)
                if date is None:
                    self.add(Level.ERROR, "date-format", table.name, f"« {column} » : « {value} » n'est pas une date JJ/MM/AAAA.",
                             item=item, cell=row.cell(header))
                    continue
                parsed[column] = date
                if date > self.today:
                    self.add(Level.WARNING, "date-future", table.name, f"« {column} » est dans le futur ({value}).", item=item, cell=row.cell(header))
            added, validated = parsed.get(spec.added_date or ""), parsed.get(spec.validated_date or "")
            if added and validated and validated < added:
                self.add(Level.WARNING, "date-ordre", table.name, "La date de validation précède la date d'ajout.", item=item,
                         cell=row.cell(table.header(spec.validated_date)))

    # Renvois entre onglets

    def _all_ids(self) -> dict[str, str]:
        return {key: name for name, table in self.tables.items() for key in table.by_id}

    def references(self) -> None:
        known = self._all_ids()
        for table in self.tables.values():
            if table.spec is None:
                continue
            id_header = table.header(table.spec.id_column)
            for row in table.rows:
                own = row.get(id_header)
                for header, text in row.values.items():
                    if header == id_header or not text:
                        continue
                    for ref in mentioned_ids(text, self.prefixes):
                        if ref in known or ref == own:
                            continue
                        strict = any(_same(header, f) for f in ID_FIELDS)
                        level = Level.ERROR if strict else Level.WARNING
                        self.add(level, "renvoi", table.name, f"« {header} » cite {ref}, qui n'existe dans aucun onglet.",
                                 item=own, cell=row.cell(header))

    # Règles de l'équipe

    def _decision_mentions(self) -> set[str]:
        """Identifiants cités par une décision ACTIVE (ou sans état), toutes colonnes confondues."""
        table = self.tables.get("DECISIONS")
        if table is None or table.spec is None:
            return set()
        state_header = table.header("État de la décision")
        mentions: set[str] = set()
        for row in table.rows:
            if state_header and row.get(state_header) and not _same(row.get(state_header), "ACTIVE"):
                continue
            for text in row.values.values():
                mentions.update(mentioned_ids(text, self.prefixes))
        return mentions

    def decisions(self) -> None:
        decided = self._decision_mentions()
        decisions_table = self.tables.get("DECISIONS")
        active_decisions: set[str] = set()
        if decisions_table is not None:
            state_header = decisions_table.header("État de la décision")
            active_decisions = {key for key, row in decisions_table.by_id.items() if not state_header or _same(row.get(state_header), "ACTIVE")}
        for table in self.tables.values():
            spec = table.spec
            if spec is None or not spec.decided_statuses or table.header_row is None:
                continue
            id_header, status_header = table.header(spec.id_column), table.header(spec.status_column)
            if id_header is None or status_header is None:
                continue
            for row in table.rows:
                key, status = row.get(id_header), row.get(status_header)
                if not key or not any(_same(status, s) for s in spec.decided_statuses):
                    continue
                own_refs = {r for t in row.values.values() for r in mentioned_ids(t, self.prefixes)}
                if key in decided or own_refs & active_decisions:
                    continue
                self.add(Level.WARNING, "decision-absente", table.name, f"Statut {status} sans décision correspondante dans DECISIONS "
                         "(le registre DECISIONS est la référence officielle).", item=key, cell=row.cell(status_header))
        table = self.tables.get("DECISIONS")
        if table is None or table.header_row is None:
            return
        state, replaces = table.header("État de la décision"), table.header("Remplace (ID décision)")
        replaced_by: dict[str, str] = {}
        if replaces:
            for row in table.rows:
                for ref in mentioned_ids(row.get(replaces), ("DEC",)):
                    replaced_by[ref] = row.get(table.header("ID"))
        for key, row in table.by_id.items():
            value = row.get(state)
            if state and _same(value, "REMPLACÉE") and key not in replaced_by:
                self.add(Level.WARNING, "decision-remplacee", table.name, "Décision REMPLACÉE, mais aucune décision n'indique la remplacer "
                         "(colonne « Remplace »).", item=key, cell=row.cell(state))
            if key in replaced_by and state and not _same(value, "REMPLACÉE"):
                self.add(Level.WARNING, "decision-remplacee", table.name, f"Remplacée par {replaced_by[key]}, mais son état est « {value or 'vide'} » "
                         "au lieu de REMPLACÉE.", item=key, cell=row.cell(state))

    def questions(self) -> None:
        table = self.tables.get("OPEN_QUESTIONS")
        if table is None or table.header_row is None:
            return
        status, answer = table.header("Statut"), table.header("Réponse apportée")
        if status is None or answer is None:
            return
        for key, row in table.by_id.items():
            if _same(row.get(status), "RÉSOLUE") and not row.get(answer):
                self.add(Level.WARNING, "question-reponse", table.name, "Question RÉSOLUE sans « Réponse apportée ».", item=key, cell=row.cell(answer))

    def changelog(self) -> None:
        table = self.tables.get("CHANGELOG")
        if table is None or table.header_row is None or not table.rows:
            return
        mentioned: set[str] = set()
        for row in table.rows:
            for text in row.values.values():
                mentioned.update(mentioned_ids(text, self.prefixes))
        for name in EXPECTED_SHEETS:
            other = self.tables.get(name)
            if other is None or other is table:
                continue
            for key, row in other.by_id.items():
                if key not in mentioned:
                    self.add(Level.WARNING, "changelog-absent", name, "Aucune ligne du CHANGELOG ne mentionne cet élément (ajout non tracé).",
                             item=key, cell=row.cell(other.header("ID")))
        date_header = table.header("Date")
        dates = [(row, parse_date(row.get(date_header))) for row in table.rows] if date_header else []
        previous = None
        for row, date in dates:
            if date is None:
                continue
            if previous is not None and date < previous:
                self.add(Level.INFO, "changelog-ordre", "CHANGELOG", "Entrée plus ancienne que la précédente (historique dans le désordre ?).",
                         cell=row.cell(date_header))
            previous = date

    # DASHBOARD

    def _count(self, sheet: str, column: str, values: Iterable[str]) -> int | None:
        table = self.tables.get(sheet)
        if table is None or table.header_row is None:
            return None
        header = table.header(column)
        if header is None:
            return None
        wanted = list(values)
        # Comme COUNTIF : texte exact (la casse mise à part), sans tolérance sur les accents.
        return sum(1 for row in table.rows if any(row.get(header).casefold() == v.casefold() for v in wanted))

    def _total(self, sheet: str) -> int | None:
        table = self.tables.get(sheet)
        if table is None or table.header_row is None:
            return None
        id_header = table.header("ID")
        return sum(1 for row in table.rows if row.get(id_header)) if id_header else len(table.rows)

    def dashboard(self) -> None:
        rows = self.raw.get("DASHBOARD")
        if not rows:
            return
        last = None
        changelog = self.tables.get("CHANGELOG")
        if changelog and changelog.header("Date"):
            parsed = [parse_date(r.get(changelog.header("Date"))) for r in changelog.rows]
            last = max((d for d in parsed if d), default=None)
        expected: dict[str, int | str | None] = {
            "Fonctionnalités (total)": self._total("FEATURES"),
            "Fonctionnalités validées": self._count("FEATURES", "Statut", DECIDED_STATUSES),
            "Idées en attente": self._count("IDEAS", "Statut", ("PROPOSÉ", "À DISCUTER", "À CLARIFIER")),
            "Décisions prises": self._count("DECISIONS", "État de la décision", ("ACTIVE",)),
            "Questions ouvertes": self._count("OPEN_QUESTIONS", "Statut", ("OUVERTE", "EN COURS")),
            "Idées (total)": self._total("IDEAS"),
            "Fonctionnalités en cours de réalisation": self._count("FEATURES", "Statut", ("EN COURS",)),
            "Décisions remplacées": self._count("DECISIONS", "État de la décision", ("REMPLACÉE",)),
            "Questions résolues": self._count("OPEN_QUESTIONS", "Statut", ("RÉSOLUE",)),
            "Dernière modification": last.strftime("%d/%m/%Y") if last else None,
        }
        labels = {norm(k): k for k in expected}
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                label = labels.get(norm(text))
                if label is None or r + 1 >= len(rows):
                    continue
                shown = rows[r + 1][c] if c < len(rows[r + 1]) else ""
                want = expected[label]
                if want is None:
                    continue
                if label == "Dernière modification":
                    shown_date = parse_date(shown) if shown else None
                    if shown_date is not None and shown_date.strftime("%d/%m/%Y") != want:
                        self.add(Level.WARNING, "dashboard", "DASHBOARD", f"« {label} » affiche {shown}, mais la dernière date du CHANGELOG est {want}.",
                                 cell=f"{column_letter(c)}{r + 2}")
                    continue
                if shown.strip() != str(want):
                    self.add(Level.WARNING, "dashboard", "DASHBOARD", f"« {label} » affiche « {shown or 'vide'} », recalculé : {want} "
                             "(formule cassée ou plage trop courte ?).", cell=f"{column_letter(c)}{r + 2}")
        self._category_table(rows)

    def _category_table(self, rows: Sequence[Sequence[str]]) -> None:
        features = self.tables.get("FEATURES")
        if features is None or features.header_row is None:
            return
        cat_h, status_h = features.header("Catégorie"), features.header("Statut")
        if cat_h is None or status_h is None:
            return
        start = next((i for i, row in enumerate(rows) if row and _same(row[0], "Catégorie")), None)
        if start is None:
            return
        statuses = list(rows[start][1:])
        for offset, row in enumerate(rows[start + 1:], start=start + 2):
            if not row or not row[0]:
                break
            category = row[0]
            total_row = _same(category, "TOTAL")
            if not total_row and not any(_same(category, c) for c in CATEGORIES):
                continue
            for c, status in enumerate(statuses, start=1):
                if not status or c >= len(row):
                    continue
                if _same(status, "Total"):
                    want = sum(1 for r in features.rows if total_row or r.get(cat_h).casefold() == category.casefold())
                else:
                    want = sum(1 for r in features.rows if (total_row or r.get(cat_h).casefold() == category.casefold())
                               and r.get(status_h).casefold() == status.casefold())
                if row[c].strip() != str(want):
                    self.add(Level.WARNING, "dashboard", "DASHBOARD", f"Répartition « {category} » / « {status} » : affiché "
                             f"« {row[c] or 'vide'} », recalculé : {want}.", cell=f"{column_letter(c)}{offset}")


def verify_workbook(sheets: Sheets, *, source: str = "classeur", today: dt.date | None = None) -> Report:
    """Vérifie le classeur (lecture seule) et renvoie le rapport."""
    checker = _Checker(sheets, today=today or dt.date.today())
    findings = checker.run()
    counts = {name: len(t.rows) for name, t in checker.tables.items() if t.spec is not None and t.header_row is not None}
    return Report(source, dt.datetime.now().strftime("%d/%m/%Y %H:%M"), findings, counts)


# --- Rendu -----------------------------------------------------------------------------------------------


def report_markdown(report: Report) -> str:
    lines = [
        "# Vérification du classeur",
        "",
        f"Source : {report.source}  ",
        f"Vérifié le : {report.checked_at}",
        "",
        report.summary(),
        "",
        "Le classeur n'a pas été modifié.",
        "",
        "| Onglet | Lignes lues |",
        "|---|---|",
        *(f"| {name} | {count} |" for name, count in report.counts.items()),
        "",
    ]
    for level, title in ((Level.ERROR, "Erreurs"), (Level.WARNING, "Avertissements"), (Level.INFO, "Informations")):
        items = [f for f in report.findings if f.level is level]
        lines += [f"## {title} ({len(items)})", ""]
        if not items:
            lines += ["Aucune.", ""]
            continue
        lines += ["| Où | Contrôle | Constat |", "|---|---|---|"]
        lines += [f"| {f.where()} | `{f.code}` | {f.message.replace('|', '/')} |" for f in items]
        lines.append("")
    return "\n".join(lines)


def finding_line(finding: Finding) -> str:
    return f"{LEVEL_ICON[finding.level]} **{finding.where()}** : {finding.message}"
