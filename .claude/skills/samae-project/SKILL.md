---
name: samae-project
description: Architecture, conventions et règles de sécurité de Samae, le bot Discord Python de l'équipe SAE EPITA. À utiliser pour toute modification du dépôt sae-project-bot (commandes, base SQLite, réunions, idées, suivi Excel, déploiement VPS).
---

# Samae : règles du projet

Samae est le bot Discord de l'équipe SAE EPITA (5 membres, projet de jeu « LUNASTEH »). Tout est en
**français** : code commenté en français, messages Discord, documentation, commits.

## Pile et commandes

- Python 3.11 à 3.13 (3.12 recommandé), **discord.py 2.7.1** (pas Pycord), aiohttp, SQLite, openpyxl.
- Lancer : `python -m src.main`. Tests : `python -m pytest -q` (tous doivent passer sur 3.11 et 3.13).
- Versions épinglées dans `requirements.txt` ; n'ajoutez une dépendance que vérifiée (licence, activité).

## Architecture (ne pas réorganiser)

- `src/services/<domaine>.py` : logique métier **sans discord.py**, testable avec des faux. Les
  permissions sont décidées ici, à partir d'un `Actor(user_id, name, is_admin, guild_id)`.
- `src/database/<domaine>.py` : seul endroit avec du SQL. Les écritures concurrentes se vérifient
  dans la transaction (`INSERT … SELECT … WHERE`, `UPDATE … WHERE état attendu`).
- `src/discord_bot/cogs/<domaine>.py` : adaptateurs (identifier la personne, appeler le service,
  répondre en éphémère). `src/discord_bot/<domaine>_ui.py` : embeds et vues.
- Migrations : liste `MIGRATIONS` dans `src/database/connection.py`, **ajouts seulement** à la fin
  (jamais DROP/DELETE/UPDATE de données existantes) ; sauvegarde automatique avant migration.
- Aide : chaque commande a une `HelpEntry` dans `src/services/help.py` (un test l'impose) ; une
  sous-commande d'admin dans un groupe public porte `extras={"admin": True}` + `@admin_only()`.
- Espace d'équipe (`HOME_ONLY_COMMANDS` dans `bot.py`) : commandes liées au serveur principal.

## Règles Discord

- Aucune mention qui notifie : `allowed_mentions=AllowedMentions.none()` partout.
- Aucun intent privilégié (ni contenu des messages, ni présences) ; « membres » seulement si
  `WELCOME_ON_JOIN`. L'assistant lit seulement les messages qui le mentionnent.
- Répondre en moins de 3 s (`defer` avant tout travail lent). Boutons persistants : `DynamicItem`.
- Les limites Discord (25 options, 1024 caractères par champ, 6000 par embed) sont dans `text_limits.py`.

## Règles de l'équipe (impératives)

- Une **décision officielle** n'est jamais créée ni modifiée sans validation humaine (bouton de
  confirmation d'un administrateur). Les votes des tier lists restent masqués ; une idée classée F
  n'est jamais éliminée automatiquement.
- Le classeur Excel original n'est **jamais** écrasé : import en lecture, export dans un nouveau fichier.
- Jamais de secret dans Git, les journaux ou Discord ; tokens seulement dans `.env` sur le serveur.
- Aucune opération destructive (suppression de données, reset de base, migration destructive).
- Aucun service payant engagé : l'IA (`AI_PROVIDER`) est facultative, tout doit marcher sans.
- Travail sur une branche dédiée, PR vers `main` ; Romain fusionne et déploie lui-même
  (`bash deploy/update.sh` sur le VPS, retour arrière `bash deploy/rollback.sh`).

## Documentation à tenir à jour

README (tableau « État actuel »), `docs/commands.md`, `docs/test-manuel.md`, `.env.example`, et le
guide du domaine (`docs/meeting-manager.md`, `docs/suivi-excel.md`, `docs/assistant.md`…).
