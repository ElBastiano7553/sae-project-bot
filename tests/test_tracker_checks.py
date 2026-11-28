"""Vérification du classeur (1.15.0) : src/tracker/checks.py, src/tracker/google.py, src/tracker/verify.py, /suivi verifier."""

from __future__ import annotations

import datetime as dt

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.tracker import verify as verify_cli
from src.tracker.checks import Level, column_letter, mentioned_ids, parse_date, report_markdown, sheets_from_xlsx, verify_workbook
from src.tracker.google import GoogleSheetError, download_xlsx, export_url, spreadsheet_id
from tests import lunasteh_fixture as fixture

TODAY = dt.date(2026, 11, 28)
SHEET_ID = "1-dr6HEmWGe1JS2kJgS9v5D-xTpDhehgA6QcQKGUChC8"


def check(data=None):
    return verify_workbook(data or fixture.sheets(), source="test", today=TODAY)


def codes(report, level=None):
    return [f.code for f in report.findings if level is None or f.level is level]


def _set(data, sheet, key, column, value):
    rows = data[sheet]
    header_row = next(i for i, r in enumerate(rows) if r and r[0] == "ID")
    col = rows[header_row].index(column)
    row = next(r for r in rows[header_row + 1:] if r and r[0] == key)
    row[col] = value


# --- Outils --------------------------------------------------------------------------------------------


def test_mentioned_ids_expand_ranges_and_ignore_unknown_prefixes():
    assert mentioned_ids("FEATURES : FEAT-001 à FEAT-003") == ["FEAT-001", "FEAT-002", "FEAT-003"]
    assert mentioned_ids("OPEN_QUESTIONS : Q-009 à Q-010, voir DEC-001") == ["Q-009", "Q-010", "DEC-001"]
    assert mentioned_ids("PR-123 et ABC-001 ne sont pas des éléments") == []
    assert mentioned_ids("FEAT-005 à FEAT-001") == ["FEAT-005", "FEAT-001"]  # plage à l'envers : pas d'expansion


def test_dates_and_columns():
    assert parse_date("26/11/2026") == dt.date(2026, 11, 26)
    assert parse_date("2026-11-26") == dt.date(2026, 11, 26)
    assert parse_date("31/02/2026") is None and parse_date("demain") is None
    assert [column_letter(i) for i in (0, 8, 25, 26)] == ["A", "I", "Z", "AA"]


# --- Classeur cohérent ----------------------------------------------------------------------------------


def test_a_coherent_workbook_has_no_finding():
    report = check()
    assert report.findings == [], [f.message for f in report.findings]
    assert report.ok and report.counts["FEATURES"] == 3 and report.counts["UI_UX"] == 0
    assert "aucune erreur" in report.summary()


def test_the_xlsx_reading_gives_the_same_result_with_real_dates_and_numbers():
    report = verify_workbook(sheets_from_xlsx(fixture.xlsx()), source="test", today=TODAY)
    assert report.findings == [], [f.message for f in report.findings]


# --- Défauts détectés -----------------------------------------------------------------------------------


def test_missing_sheet_and_renamed_column():
    data = fixture.sheets()
    del data["UI_UX"]
    data["IDEAS"][1][7] = "État"  # « Statut » renommé
    report = check(data)
    assert ("onglet", "UI_UX") in {(f.code, f.sheet) for f in report.errors}
    assert any(f.code == "colonne" and "Statut" in f.message for f in report.warnings)


def test_identifier_problems():
    data = fixture.sheets()
    data["FEATURES"].append(fixture._feature("FEAT-002", "Doublon", "Gameplay", "PROPOSÉ"))
    data["FEATURES"].append(fixture._feature("F-9", "Mauvais format", "Gameplay", "PROPOSÉ"))
    data["FEATURES"].append(fixture._feature("", "Sans identifiant", "Gameplay", "PROPOSÉ"))
    data["FEATURES"].append(fixture._feature("FEAT-007", "Après un trou", "Gameplay", "PROPOSÉ"))
    report = check(data)
    found = {(f.code, f.item) for f in report.findings}
    assert ("id-double", "FEAT-002") in found
    assert ("id-format", "F-9") in found
    assert any(f.code == "id-manquant" and f.cell == "A8" for f in report.errors)
    trou = next(f for f in report.infos if f.code == "id-trou")
    assert "FEAT-004" in trou.message and "FEAT-006" in trou.message


def test_values_outside_the_dropdown_lists():
    data = fixture.sheets()
    _set(data, "FEATURES", "FEAT-002", "Statut", "Fini")
    _set(data, "FEATURES", "FEAT-001", "Priorité", "Urgente")
    _set(data, "IDEAS", "IDEA-001", "Statut", "Proposé")  # même mot, mauvaise casse et accents ignorés par COUNTIF ?
    report = check(data)
    errors = {(f.item, f.cell) for f in report.errors if f.code == "liste"}
    assert ("FEAT-002", "I4") in errors and ("FEAT-001", "H3") in errors
    assert any(f.code == "liste" and f.item == "IDEA-001" and "PROPOSÉ" in f.message for f in report.warnings)


def test_required_fields_and_dates():
    data = fixture.sheets()
    _set(data, "FEATURES", "FEAT-001", "Catégorie", "")
    _set(data, "FEATURES", "FEAT-002", "Date d'ajout", "le 3 mars")
    _set(data, "FEATURES", "FEAT-003", "Date de validation", "01/11/2026")  # avant l'ajout
    _set(data, "IDEAS", "IDEA-001", "Date d'ajout", "01/01/2030")
    report = check(data)
    assert {"obligatoire", "date-format", "date-ordre", "date-future"} <= set(codes(report))
    assert next(f for f in report.errors if f.code == "date-format").cell == "J4"


def test_broken_references():
    data = fixture.sheets()
    _set(data, "FEATURES", "FEAT-002", "Interactions avec les autres fonctionnalités", "Voir Q-099 et FEAT-001.")
    _set(data, "DECISIONS", "DEC-001", "Remplace (ID décision)", "DEC-077")
    report = check(data)
    assert any(f.code == "renvoi" and "Q-099" in f.message for f in report.warnings)
    assert any(f.code == "renvoi" and "DEC-077" in f.message for f in report.errors)


def test_a_validated_item_needs_a_decision():
    data = fixture.sheets()
    _set(data, "FEATURES", "FEAT-002", "Statut", "VALIDÉ")  # aucune décision ne cite FEAT-002
    data["DASHBOARD"][4][2] = "3"
    data["DASHBOARD"][12][1], data["DASHBOARD"][12][4] = "0", "2"
    data["DASHBOARD"][14][1], data["DASHBOARD"][14][4] = "0", "2"
    report = check(data)
    assert [(f.code, f.item) for f in report.findings] == [("decision-absente", "FEAT-002")]


def test_a_cancelled_decision_no_longer_covers_its_items():
    data = fixture.sheets()
    _set(data, "DECISIONS", "DEC-001", "État de la décision", "ANNULÉE")
    data["DASHBOARD"][4][6] = "1"
    report = check(data)
    assert ("decision-absente", "FEAT-001") in {(f.code, f.item) for f in report.findings}


def test_replaced_decisions_must_be_consistent():
    data = fixture.sheets()
    data["DECISIONS"].append(["DEC-003", "Saut", "Saut plus haut.", "Test.", fixture.DATE, "", "FEAT-001", "ACTIVE", "DEC-001", "Réunion"])
    data["CHANGELOG"].append([fixture.DATE, "Remplacement", "DECISIONS : DEC-003", "", "", "Réunion"])
    data["DASHBOARD"][4][6] = "3"
    report = check(data)
    assert ("decision-remplacee", "DEC-001") in {(f.code, f.item) for f in report.findings}


def test_resolved_question_without_answer():
    data = fixture.sheets()
    _set(data, "OPEN_QUESTIONS", "Q-002", "Réponse apportée", "")
    report = check(data)
    assert [(f.code, f.item) for f in report.findings] == [("question-reponse", "Q-002")]


def test_items_missing_from_the_changelog():
    data = fixture.sheets()
    data["CHANGELOG"][3][2] = "FEATURES : FEAT-001 à FEAT-002"
    report = check(data)
    assert [(f.code, f.item) for f in report.findings] == [("changelog-absent", "FEAT-003")]


def test_dashboard_counters_are_recomputed():
    data = fixture.sheets()
    data["DASHBOARD"][4][0] = "2"  # « Fonctionnalités (total) » : formule sur une plage trop courte, par exemple
    data["DASHBOARD"][13][5] = "0"  # Progression / EN COURS
    data["DASHBOARD"][8][8] = "01/11/2026"  # Dernière modification
    report = check(data)
    dashboard = [f for f in report.findings if f.code == "dashboard"]
    assert {f.cell for f in dashboard} == {"A5", "F14", "I9"}


def test_markdown_report_lists_every_finding():
    data = fixture.sheets()
    _set(data, "FEATURES", "FEAT-002", "Statut", "Fini")
    text = report_markdown(check(data))
    assert "## Erreurs (1)" in text and "FEATURES · FEAT-002 · I4" in text and "Le classeur n'a pas été modifié." in text


# --- Google Sheet ---------------------------------------------------------------------------------------


def test_spreadsheet_id_accepts_only_google_addresses():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?usp=sharing"
    assert spreadsheet_id(url) == SHEET_ID and spreadsheet_id(SHEET_ID) == SHEET_ID
    assert export_url(url) == f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
    for bad in ("https://evil.example/spreadsheets/d/" + SHEET_ID, "http://docs.google.com/spreadsheets/d/" + SHEET_ID, "abc", ""):
        with pytest.raises(GoogleSheetError):
            spreadsheet_id(bad)


async def _serve(handler):
    app = web.Application()
    app.router.add_get("/export", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_download_returns_the_xlsx():
    data = fixture.xlsx()

    async def export(request):
        return web.Response(body=data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    server = await _serve(export)
    try:
        assert await download_xlsx(SHEET_ID, url=str(server.make_url("/export"))) == data
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [(403, b"", "Partager"), (404, b"", "introuvable"), (200, b"<html>Connexion</html>", "privé"), (500, b"", "HTTP 500")],
)
async def test_download_explains_why_the_sheet_is_unreadable(status, body, message):
    async def export(request):
        return web.Response(status=status, body=body)

    server = await _serve(export)
    try:
        with pytest.raises(GoogleSheetError, match=message):
            await download_xlsx(SHEET_ID, url=str(server.make_url("/export")))
    finally:
        await server.close()


# --- Ligne de commande ----------------------------------------------------------------------------------


def test_command_line_exit_codes(tmp_path, capsys):
    good = tmp_path / "bon.xlsx"
    good.write_bytes(fixture.xlsx())
    assert verify_cli.main([str(good), "--markdown", str(tmp_path / "r.md"), "--json", str(tmp_path / "r.json")]) == 0
    assert "aucune erreur" in (tmp_path / "r.md").read_text(encoding="utf-8")
    data = fixture.sheets()
    _set(data, "FEATURES", "FEAT-002", "Statut", "Fini")
    bad = tmp_path / "mauvais.xlsx"
    bad.write_bytes(fixture.xlsx(data))
    assert verify_cli.main([str(bad), "--markdown", str(tmp_path / "r2.md")]) == 1
    broken = tmp_path / "casse.xlsx"
    broken.write_bytes(b"pas un classeur")
    assert verify_cli.main([str(broken)]) == 2
    assert verify_cli.main([str(tmp_path / "absent.xlsx")]) == 2
    assert verify_cli.main(["--google", "https://evil.example/x"]) == 2
    capsys.readouterr()


# --- Réglage TRACKER_GOOGLE_SHEET -----------------------------------------------------------------------


def test_setting_keeps_only_the_identifier(tmp_path):
    from src.config import ConfigError, Settings

    base = {"DISCORD_BOT_TOKEN": "x" * 60, "DATABASE_PATH": str(tmp_path / "b.db")}
    settings = Settings.from_env({**base, "TRACKER_GOOGLE_SHEET": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
                                 project_root=tmp_path)
    assert settings.tracker_google_sheet == SHEET_ID
    with pytest.raises(ConfigError, match="TRACKER_GOOGLE_SHEET"):
        Settings.from_env({**base, "TRACKER_GOOGLE_SHEET": "https://exemple.invalid/classeur"}, project_root=tmp_path)


def test_levels_are_ordered_errors_first():
    data = fixture.sheets()
    _set(data, "FEATURES", "FEAT-002", "Statut", "Fini")
    _set(data, "OPEN_QUESTIONS", "Q-002", "Réponse apportée", "")
    data["FEATURES"].append(fixture._feature("FEAT-009", "Trou", "Gameplay", "PROPOSÉ"))
    levels = [f.level for f in check(data).findings]
    assert levels == sorted(levels, key=[Level.ERROR, Level.WARNING, Level.INFO].index)
