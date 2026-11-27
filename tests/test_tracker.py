"""Suivi du projet (classeur Excel) : analyse, import, propositions validées par un humain, export sans écraser l'original."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook, load_workbook

from src.database import Database
from src.services.meetings import Actor, PermissionDenied
from src.services.tracker import DECISIONS, FEATURES, IDEAS, OPEN_QUESTIONS, TrackerError, TrackerService
from src.tracker.inspect import main as inspect_main
from src.tracker.workbook import inspect_workbook, read_rows, report_markdown
from tests.tracker_fixture import build_workbook

ADMIN = Actor(1, "Romain", True, 42)
BOB = Actor(2, "Bob", False, 42)


@pytest.fixture
def workbook() -> bytes:
    return build_workbook()


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "sae_bot.sqlite3")
    yield TrackerService(db, tmp_path / "tracker")
    db.close()


@pytest.fixture
def imported(service, workbook):
    service.apply_import(workbook, "LUNASTEH.xlsx", ADMIN)
    return service


# --- Analyse ---------------------------------------------------------------------------------


def test_inspection_finds_headers_ids_statuses_formulas_and_validations(workbook):
    info = inspect_workbook(workbook, "LUNASTEH.xlsx")
    assert [s.name for s in info.sheets][:2] == ["DASHBOARD", "FEATURES"] and info.missing_sheets == []
    features = info.sheet("FEATURES")
    assert features.header_row == 2  # titre en ligne 1, en-têtes en ligne 2
    assert (features.id_column, features.title_column, features.status_column) == ("ID", "Fonctionnalité", "Statut")
    assert features.id_pattern == "F-###" and features.data_rows == 3
    assert features.formula_headers == ["Points x2"]
    assert features.column("Statut").validation == ["À faire", "En cours", "Terminé", "Abandonné"]
    dashboard = info.sheet("DASHBOARD")
    assert dashboard.formula_cells == 2 and dashboard.cross_references == {"FEATURES": 1, "OPEN_QUESTIONS": 1}
    report = report_markdown(info)
    assert "## FEATURES" in report and "`=E3*2`" in report


def test_inspection_reports_duplicates_and_missing_sheets():
    wb = Workbook()
    ws = wb.active
    ws.title = "FEATURES"
    ws.append(["ID", "Nom", "Statut"])
    ws.append(["F-1", "A", "Fait"])
    ws.append(["F-1", "B", "Fait"])
    buffer = io.BytesIO()
    wb.save(buffer)
    info = inspect_workbook(buffer.getvalue())
    assert info.sheet("FEATURES").duplicate_ids == ["F-1"]
    assert "DECISIONS" in info.missing_sheets
    rows = read_rows(buffer.getvalue(), info)["FEATURES"]
    assert [key for _, key, _ in rows] == ["F-1", "ligne-3"]  # doublon : clé de secours, aucune ligne perdue


def test_inspect_command_line_never_modifies_the_file(tmp_path, workbook, capsys):
    path = tmp_path / "GAME DESIGN DATABASE — LUNASTEH.xlsx"
    path.write_bytes(workbook)
    before = path.read_bytes()
    assert inspect_main([str(path), "--json", str(tmp_path / "a.json")]) == 0
    assert path.read_bytes() == before
    assert "## OPEN_QUESTIONS" in capsys.readouterr().out
    assert inspect_main([str(tmp_path / "absent.xlsx")]) == 2


# --- Import ----------------------------------------------------------------------------------


def test_nothing_works_before_import(service):
    with pytest.raises(TrackerError, match="importé"):
        service.progress()


def test_import_requires_admin_backs_up_and_keeps_original(service, workbook, tmp_path):
    with pytest.raises(PermissionDenied):
        service.apply_import(workbook, "LUNASTEH.xlsx", BOB)
    result = service.apply_import(workbook, "LUNASTEH.xlsx", ADMIN)
    assert result["new"] == 11 and result["changed"] == 0
    originals = list((tmp_path / "tracker" / "originals").iterdir())
    assert len(originals) == 1 and originals[0].read_bytes() == workbook
    assert list((tmp_path / "backups" / "suivi").glob("*avantimportsuivi*.sqlite3"))


def test_progress_and_queries(imported):
    p = imported.progress()
    assert (p["features"], p["done"], p["percent"], p["open_questions"]) == (3, 1, 33, 1)
    assert [r.key for r in imported.by_status(FEATURES, remaining=True)] == ["F-002", "F-003"]
    assert [r.key for r in imported.by_status(OPEN_QUESTIONS, open_only=True)] == ["Q-1"]
    assert [r.key for r in imported.search("inventaire")] == ["F-002"]
    assert imported.allowed_statuses(FEATURES) == ["À faire", "En cours", "Terminé", "Abandonné"]


def test_reimport_never_deletes_and_keeps_samae_changes(imported, workbook):
    change = imported.propose_update(FEATURES, "F-002", BOB, "Statut", "Terminé")
    imported.approve(change.id, ADMIN)
    _, _, diff = imported.preview_import(workbook, "LUNASTEH.xlsx")
    assert diff["kept"] == 1 and diff["changed"] == 0
    # Classeur sans la ligne F-003 : elle reste dans Samae.
    wb = load_workbook(io.BytesIO(workbook))
    wb["FEATURES"].delete_rows(5)
    buffer = io.BytesIO()
    wb.save(buffer)
    result = imported.apply_import(buffer.getvalue(), "LUNASTEH.xlsx", ADMIN)
    assert result["missing"] == 1
    assert imported.repo.get_row(FEATURES, "F-003") is not None
    assert imported.repo.get_row(FEATURES, "F-002").data["Statut"] == "Terminé"


# --- Propositions ----------------------------------------------------------------------------


def test_ideas_and_questions_are_added_directly_with_next_identifier(imported):
    change = imported.propose_add(IDEAS, BOB, title="Mode photo", description="capturer l'écran")
    assert change.status == "applied" and change.row_key == "I-02"
    row = imported.repo.get_row(IDEAS, "I-02")
    assert row.origin == "samae" and row.data["Auteur"] == "Bob" and row.data["Description"] == "capturer l'écran"
    assert imported.propose_add(OPEN_QUESTIONS, BOB, title="Combien de niveaux ?").status == "applied"


def test_decisions_always_wait_for_an_admin(imported):
    change = imported.propose_add(DECISIONS, ADMIN, title="Résolution 1280×720")
    assert change.status == "pending"  # même proposée par un admin : validation explicite
    with pytest.raises(PermissionDenied):
        imported.approve(change.id, BOB)
    applied = imported.approve(change.id, ADMIN)
    assert applied.status == "applied" and applied.row_key == "D-02" and applied.decider == "Romain"
    with pytest.raises(TrackerError, match="plus en attente"):
        imported.approve(change.id, ADMIN)


def test_validated_debate_decision_is_recorded_directly(imported):
    change = imported.propose_add(DECISIONS, ADMIN, title="On garde le coop", validated_by=ADMIN)
    assert change.status == "applied"


def test_status_updates_are_checked_and_conflicts_detected(imported):
    with pytest.raises(TrackerError, match="Statut inconnu"):
        imported.propose_update(FEATURES, "F-003", BOB, "Statut", "Fini")
    with pytest.raises(TrackerError, match="formule"):
        imported.propose_update(FEATURES, "F-003", BOB, "Points x2", "8")
    with pytest.raises(TrackerError, match="identifiant"):
        imported.propose_update(FEATURES, "F-003", BOB, "ID", "F-999")
    first = imported.propose_update(FEATURES, "F-003", BOB, "Statut", "En cours")
    second = imported.propose_update(FEATURES, "F-003", BOB, "Statut", "Abandonné")
    imported.approve(first.id, ADMIN)
    with pytest.raises(TrackerError, match="a changé"):
        imported.approve(second.id, ADMIN)
    assert imported.repo.get_row(FEATURES, "F-003").data["Statut"] == "En cours"


def test_reject_by_admin_or_author_only(imported):
    change = imported.propose_update(FEATURES, "F-003", BOB, "Statut", "En cours")
    with pytest.raises(PermissionDenied):
        imported.reject(change.id, Actor(3, "Chloé", False, 42))
    imported.reject(change.id, BOB)
    assert imported.repo.change(change.id).status == "rejected"


# --- Export ----------------------------------------------------------------------------------


def test_export_writes_a_new_file_and_preserves_formulas(imported, workbook, tmp_path):
    imported.propose_add(IDEAS, BOB, title="Mode photo")
    change = imported.propose_update(FEATURES, "F-002", BOB, "Statut", "Terminé")
    imported.approve(change.id, ADMIN)
    with pytest.raises(PermissionDenied):
        imported.export(BOB)
    result = imported.export(ADMIN)
    original = next((tmp_path / "tracker" / "originals").iterdir())
    assert result.path != original and original.read_bytes() == workbook
    wb = load_workbook(result.path)
    features = wb["FEATURES"]
    assert features["D4"].value == "Terminé"
    assert [features[f"F{r}"].value for r in (3, 4, 5)] == ["=E3*2", "=E4*2", "=E5*2"]
    assert wb["DASHBOARD"]["B3"].value == '=COUNTIF(FEATURES!D:D,"Terminé")'
    ideas = [row for row in wb["IDEAS"].iter_rows(values_only=True)]
    assert ideas[-1][:2] == ("I-02", "Mode photo")
    changelog = [row[1] for row in wb["CHANGELOG"].iter_rows(min_row=2, values_only=True)]
    assert changelog[0] == "Création du classeur" and any("FEATURES F-002" in (c or "") for c in changelog)
    assert result.skipped_formula_cells == 3


def test_export_copies_formulas_for_new_rows(imported, tmp_path):
    change = imported.propose_add(FEATURES, BOB, title="Sauvegarde", status="À faire")
    imported.approve(change.id, ADMIN)
    wb = load_workbook(imported.export(ADMIN).path)
    features = wb["FEATURES"]
    assert features["A6"].value == "F-004" and features["F6"].value == "=E6*2"
