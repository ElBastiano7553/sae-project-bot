---
name: samae-game-design
description: Structure, relations et règles de modification du classeur « GAME DESIGN DATABASE — LUNASTEH » (Google Sheet / .xlsx) de l'équipe, et de son intégration dans Samae (/suivi). À utiliser avant de lire, vérifier, importer, exporter ou coder quoi que ce soit qui touche à ce classeur.
---

# Classeur « GAME DESIGN DATABASE — LUNASTEH »

Source de référence de l'équipe pour le game design. Il existe en Google Sheet (lien de partage dans
`TRACKER_GOOGLE_SHEET`) et en copie .xlsx. **Ne jamais le modifier** : ni le Google Sheet, ni le
fichier original. Samae lit, vérifie, importe une copie et exporte dans un **nouveau** fichier.

## Structure (lue le 28/11/2026)

Chaque onglet de données : ligne 1 = titre, ligne 2 = en-têtes, données à partir de la ligne 3.

| Onglet | Identifiants | Statut (liste déroulante) | Rôle |
|---|---|---|---|
| DASHBOARD | — | — | Compteurs calculés par formules (`COUNTA`, `COUNTIF`) à partir des autres onglets ; ne jamais écrire dedans |
| FEATURES | `FEAT-001` | PROPOSÉ, À DISCUTER, À CLARIFIER, VALIDÉ, EN COURS, TERMINÉ, ABANDONNÉ | Fonctionnalités ; Catégorie (liste du DASHBOARD A13:A24), Priorité (Haute, Moyenne, Basse), Date d'ajout, Date de validation |
| IDEAS | `IDEA-001` | PROPOSÉ, À DISCUTER, À CLARIFIER, VALIDÉ, ABANDONNÉ | Idées ; colonne D « Origine » masquée mais conservée |
| DECISIONS | `DEC-001` | État : ACTIVE, REMPLACÉE, ANNULÉE | **Registre officiel** des décisions ; « Remplace (ID décision) » |
| GAMEPLAY | `GP-001` | comme FEATURES | Mécaniques ; Domaine (liste) |
| UI_UX | (vide pour l'instant) | comme FEATURES | Écrans, menus |
| ART_DIRECTION | `ART-001` | comme FEATURES | Direction artistique ; Type (liste) |
| OPEN_QUESTIONS | `Q-001` | OUVERTE, EN COURS, RÉSOLUE, ABANDONNÉE | Questions ; « Réponse apportée », « Décision liée (ID) » |
| CHANGELOG | — | Type : Ajout, Complément, Modification, Décision, Remplacement, Abandon, Correction, Structure | Historique de toutes les modifications |

Relations : les textes citent des identifiants (« voir Q-004 », « FEAT-001 à FEAT-008 ») ; une ligne
VALIDÉ / EN COURS / TERMINÉ doit être couverte par une décision ACTIVE de DECISIONS ; toute ajout ou
modification doit avoir sa ligne dans CHANGELOG. VALIDÉ = accepté par l'équipe, **pas** réalisé (c'est
TERMINÉ). Les compteurs du DASHBOARD comparent le texte exact des statuts (majuscules et accents).

## Règles de travail

- Ne rien supposer sur le contenu : ne jamais inventer de ligne, de statut ou de décision. Une
  information ambiguë devient une question (OPEN_QUESTIONS), pas une interprétation.
- Une décision n'est jamais enregistrée sans validation explicite de l'équipe.
- Toute modification proposée par Samae passe par `TrackerService.propose_update` / `propose_add`, puis
  `/suivi valider` par un admin (sauvegarde de la base avant).
- Les formules ne sont jamais écrasées ; l'export recopie les formules sur les nouvelles lignes.
- Pour un test, utiliser `tests/lunasteh_fixture.py` (structure réelle, contenu inventé) ; ne jamais
  mettre le vrai contenu du classeur dans le dépôt.

## Vérifier

- `python -m src.tracker.verify --google <adresse>` ou `fichier.xlsx` (codes de sortie 0/1/2), ou
  `/suivi verifier` sur Discord. Contrôles et codes : `docs/suivi-excel.md#vérification-du-classeur-1150`.
- Structure d'un fichier inconnu : `python -m src.tracker.inspect fichier.xlsx --markdown analyse.md`.
- Pour lire le Google Sheet depuis Claude Code, le connecteur Google Sheets suffit (lecture seule ; ne
  pas utiliser ses outils d'écriture sur ce classeur sans demande explicite de Romain).

## Code concerné

`src/tracker/` (lecture, analyse, vérification, Google), `src/services/tracker.py` (import, propositions,
export), `src/database/tracker.py`, `src/discord_bot/cogs/tracker.py`, `src/services/repo_sync.py`
(liens avec le code GitHub).
