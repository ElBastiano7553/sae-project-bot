"""Classeur de TEST (structure inventée pour les tests automatiques uniquement).

Ce n'est PAS une copie du vrai « GAME DESIGN DATABASE — LUNASTEH.xlsx », qui n'était pas accessible :
il sert seulement à vérifier l'analyse, l'import et l'export (formules, listes déroulantes, identifiants).
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.worksheet.datavalidation import DataValidation


def build_workbook() -> bytes:
    wb = Workbook()
    dash = wb.active
    dash.title = "DASHBOARD"
    dash["A1"] = "Tableau de bord"
    dash["A3"], dash["B3"] = "Fonctionnalités terminées", '=COUNTIF(FEATURES!D:D,"Terminé")'
    dash["A4"], dash["B4"] = "Questions ouvertes", '=COUNTIF(OPEN_QUESTIONS!C:C,"Ouverte")'

    features = wb.create_sheet("FEATURES")
    features.append(["Suivi des fonctionnalités"])
    features.append(["ID", "Fonctionnalité", "Priorité", "Statut", "Points", "Points x2"])
    for i, (name, prio, status, pts) in enumerate(
        [("Déplacement du joueur", "Haute", "Terminé", 3), ("Inventaire", "Moyenne", "En cours", 5), ("Menu pause", "Basse", "À faire", 2)],
        start=1,
    ):
        row = 2 + i
        features.append([f"F-{i:03d}", name, prio, status, pts, f"=E{row}*2"])
    dv = DataValidation(type="list", formula1='"À faire,En cours,Terminé,Abandonné"')
    features.add_data_validation(dv)
    dv.add("D3:D200")

    ideas = wb.create_sheet("IDEAS")
    ideas.append(["ID", "Idée", "Description", "Auteur", "Date", "Statut"])
    ideas.append(["I-01", "Mode coopératif", "Deux joueurs en local", "Alice", "2026-10-01", "Proposée"])

    decisions = wb.create_sheet("DECISIONS")
    decisions.append(["ID", "Décision", "Date", "Statut"])
    decisions.append(["D-01", "Moteur : Pygame", "2026-09-20", "Validée"])

    for name in ("GAMEPLAY", "UI_UX", "ART_DIRECTION"):
        ws = wb.create_sheet(name)
        ws.append(["ID", "Élément", "Statut", "Notes"])
        ws.append([f"{name[:2]}-1", f"Premier élément {name}", "À faire", ""])

    questions = wb.create_sheet("OPEN_QUESTIONS")
    questions.append(["ID", "Question", "Statut", "Réponse"])
    questions.append(["Q-1", "Quelle résolution cible ?", "Ouverte", ""])
    questions.append(["Q-2", "Musique originale ?", "Résolue", "Oui"])

    changelog = wb.create_sheet("CHANGELOG")
    changelog.append(["Date", "Modification", "Auteur"])
    changelog.append(["2026-10-02", "Création du classeur", "Romain"])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
