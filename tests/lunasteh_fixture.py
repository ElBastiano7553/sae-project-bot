"""Classeur de TEST qui reprend la STRUCTURE du Google Sheet « GAME DESIGN DATABASE — LUNASTEH ».

Onglets, ligne de titre, en-têtes et listes déroulantes sont ceux du vrai classeur (lu le 28/11/2026) ;
le contenu des lignes est inventé pour les tests et cohérent : ``verify_workbook`` n'y trouve rien
à redire. Chaque test y introduit ensuite le défaut qu'il veut détecter.
"""

from __future__ import annotations

import copy
import datetime as dt
import io

DATE = "20/11/2026"

FEATURES_HEADERS = [
    "ID", "Nom de la fonctionnalité", "Catégorie", "Description détaillée", "Fonctionnement précis", "Règles associées",
    "Interactions avec les autres fonctionnalités", "Priorité", "Statut", "Date d'ajout", "Date de validation",
    "Notes complémentaires", "Source (provenance)",
]
IDEAS_HEADERS = ["ID", "Nom de l'idée", "Description", "Origine de l'idée", "Catégorie", "Intérêt potentiel", "Points à discuter", "Statut", "Date d'ajout"]
DECISIONS_HEADERS = [
    "ID", "Sujet", "Décision prise", "Justification", "Date", "Alternatives envisagées", "Fonctionnalités concernées",
    "État de la décision", "Remplace (ID décision)", "Source (provenance)",
]
GAMEPLAY_HEADERS = ["ID", "Domaine", "Nom", "Description / règle", "Détails et valeurs", "Fonctionnalités liées", "Statut", "Date d'ajout", "Source (provenance)", "Notes"]
UI_HEADERS = [
    "ID", "Type d'élément", "Nom", "Description", "Fonctionnement", "Navigation / éléments liés", "Fonctionnalités liées", "Statut",
    "Date d'ajout", "Source (provenance)", "Notes",
]
ART_HEADERS = ["ID", "Type", "Nom", "Description", "Valeur / référence", "Statut", "Date d'ajout", "Source (provenance)", "Notes"]
QUESTIONS_HEADERS = ["ID", "Question", "Contexte", "Raisons de la question", "Personnes concernées", "Date", "Statut", "Réponse apportée", "Décision liée (ID)"]
CHANGELOG_HEADERS = ["Date", "Type de modification", "Élément concerné", "Ancienne information", "Nouvelle information", "Origine de la modification"]


def _feature(key, name, category, status, interactions="", validated=""):
    return [key, name, category, "Description de test.", "", "", interactions, "Moyenne", status, DATE, validated, "", "Test"]


def sheets() -> dict[str, list[list[str]]]:
    """Classeur cohérent (valeurs telles qu'affichées)."""
    data = {
        "DASHBOARD": [
            ["LUNASTEH  ·  GAME DESIGN DATABASE"],
            ["Tableau de bord calculé automatiquement."],
            [],
            ["Fonctionnalités (total)", "", "Fonctionnalités validées", "", "Idées en attente", "", "Décisions prises", "", "Questions ouvertes"],
            ["3", "", "2", "", "1", "", "2", "", "1"],
            ["", "", "", "", "", "", "", "", ""],
            [],
            ["Idées (total)", "", "Fonctionnalités en cours de réalisation", "", "Décisions remplacées", "", "Questions résolues", "", "Dernière modification"],
            ["2", "", "1", "", "0", "", "1", "", DATE],
            [],
            ["Répartition des fonctionnalités par catégorie et statut"],
            ["Catégorie", "PROPOSÉ", "À DISCUTER", "À CLARIFIER", "VALIDÉ", "EN COURS", "TERMINÉ", "ABANDONNÉ", "Total"],
            ["Gameplay", "1", "0", "0", "1", "0", "0", "0", "2"],
            ["Progression", "0", "0", "0", "0", "1", "0", "0", "1"],
            ["TOTAL", "1", "0", "0", "1", "1", "0", "0", "3"],
        ],
        "FEATURES": [
            ["FEATURES  ·  Base de données des fonctionnalités"],
            FEATURES_HEADERS,
            _feature("FEAT-001", "Saut", "Gameplay", "VALIDÉ", "Lié à FEAT-002."),
            _feature("FEAT-002", "Double saut", "Gameplay", "PROPOSÉ", "Dépend de FEAT-001 (voir Q-001)."),
            _feature("FEAT-003", "Arbre", "Progression", "EN COURS", "", validated="21/11/2026"),
        ],
        "IDEAS": [
            ["IDEAS  ·  Toutes les idées proposées"],
            IDEAS_HEADERS,
            ["IDEA-001", "Pêche", "Mini-jeu.", "Réunion", "Gameplay", "", "", "PROPOSÉ", DATE],
            ["IDEA-002", "Photo", "Mode photo.", "Réunion", "Autre", "", "", "ABANDONNÉ", DATE],
        ],
        "DECISIONS": [
            ["DECISIONS  ·  Registre officiel des décisions de l'équipe"],
            DECISIONS_HEADERS,
            ["DEC-001", "Saut", "Le saut est retenu.", "Base du jeu.", DATE, "", "FEAT-001", "ACTIVE", "", "Réunion"],
            ["DEC-002", "Arbre", "L'arbre est retenu.", "Progression.", DATE, "", "FEAT-003 ; GP-001", "ACTIVE", "", "Réunion"],
        ],
        "GAMEPLAY": [
            ["GAMEPLAY  ·  Mécaniques et règles de gameplay"],
            GAMEPLAY_HEADERS,
            ["GP-001", "Progression", "Début", "Règle de test.", "", "FEAT-003", "VALIDÉ", DATE, "Test", ""],
        ],
        "UI_UX": [["UI_UX  ·  Écrans, menus, navigation et interactions"], UI_HEADERS],
        "ART_DIRECTION": [
            ["ART_DIRECTION  ·  Direction artistique"],
            ART_HEADERS,
            ["ART-001", "Ambiance", "Nuit", "Ambiance nocturne.", "Nuit", "PROPOSÉ", DATE, "Test", ""],
        ],
        "OPEN_QUESTIONS": [
            ["OPEN_QUESTIONS  ·  Questions nécessitant une réponse de l'équipe"],
            QUESTIONS_HEADERS,
            ["Q-001", "Hauteur du saut ?", "FEAT-002", "", "Équipe", DATE, "OUVERTE", "", ""],
            ["Q-002", "Moteur ?", "", "", "Équipe", DATE, "RÉSOLUE", "Godot.", "DEC-001"],
        ],
        "CHANGELOG": [
            ["CHANGELOG  ·  Historique de toutes les modifications de la base"],
            CHANGELOG_HEADERS,
            [DATE, "Structure", "Base de données (9 onglets)", "Aucune", "Création", "Équipe"],
            [DATE, "Ajout", "FEATURES : FEAT-001 à FEAT-003", "Aucune", "3 entrées", "Réunion"],
            [DATE, "Ajout", "IDEAS : IDEA-001, IDEA-002 ; GP-001 ; ART-001", "Aucune", "Entrées", "Réunion"],
            [DATE, "Décision", "DECISIONS : DEC-001 et DEC-002", "Aucune", "Décisions", "Réunion"],
            [DATE, "Ajout", "OPEN_QUESTIONS : Q-001 à Q-002", "Aucune", "Questions", "Réunion"],
        ],
    }
    return copy.deepcopy(data)


def xlsx(data: dict[str, list[list[str]]] | None = None, *, typed_dates: bool = True, validations: bool = True) -> bytes:
    """Le même classeur en .xlsx ; les dates JJ/MM/AAAA deviennent de vraies dates Excel (comme l'export Google)."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in (data or sheets()).items():
        ws = wb.create_sheet(name)
        for row in rows:
            values = []
            for value in row:
                if typed_dates and isinstance(value, str) and len(value) == 10 and value[2] == "/" and value[5] == "/":
                    day, month, year = value.split("/")
                    values.append(dt.datetime(int(year), int(month), int(day)))
                elif isinstance(value, str) and value.isdigit() and name == "DASHBOARD":
                    values.append(int(value))
                else:
                    values.append(value or None)
            ws.append(values)
        if validations:
            _add_status_list(ws, rows)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _add_status_list(ws, rows) -> None:
    """Liste déroulante sur la colonne de statut (comme le Google Sheet exporté en .xlsx)."""
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    from src.tracker.checks import SPEC_BY_NAME

    spec = SPEC_BY_NAME.get(ws.title)
    if spec is None or spec.status_column is None or len(rows) < 2 or spec.status_column not in rows[1]:
        return
    values = spec.lists[spec.status_column]
    letter = get_column_letter(rows[1].index(spec.status_column) + 1)
    dv = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"{letter}3:{letter}1000")
