# Rapport : audit du socle de Samae et version 1.15.0

Mission du 28/11/2026, faite en autonomie sur le PC de Romain (`D:\Desktop\claude\samae`).
Branche **`claude/socle-1.15`**, poussée sur GitHub, **sans pull request ouverte** et **rien de déployé**.

## En bref

| | État |
|---|---|
| Socle | Files de notifications regroupées, configuration découpée, 4 défauts de l'audit d'octobre corrigés, vérification statique (ruff) ajoutée à la CI |
| Vérification du classeur | `/suivi verifier` et `python -m src.tracker.verify` ; testé sur la structure du vrai Google Sheet : **0 erreur, 5 avertissements** |
| Synchronisation GitHub | `/suivi synchroniser` et `/suivi code` : commits et PR reliés aux lignes du suivi, propositions de statut validées par un admin |
| Tests | **1212 réussis**, 4 ignorés (Python 3.13 sur le PC) ; 1150 au départ sur la branche de la PR #26 |
| Pas fait | Rien testé sur un vrai serveur Discord ; Python 3.11 et 3.12 seront vérifiés par la CI à l'ouverture de la PR ; aucune skill externe installée |

## 1. Point de départ

- `D:\Desktop\claude\samae` était **vide** : le dépôt y a été cloné depuis GitHub (`main` = 1.13.0, commit `16d90b3`).
- La **PR #26 (1.14.0) est toujours ouverte** (sa référence `merge` existe encore sur GitHub). Comme la
  vérification du classeur s'appuie sur le suivi Excel de la 1.14.0, la branche `claude/socle-1.15`
  part de la branche de la PR #26. **Il faut donc fusionner #26 d'abord**, puis cette branche (ou
  seulement cette branche, qui contient aussi la 1.14.0).
- Sauvegarde avant toute modification : `D:\Desktop\claude\samae-sauvegardes\sae-project-bot-<date>.bundle`
  (toutes les branches du dépôt GitHub, restaurable avec `git clone <fichier>.bundle`).
- Anciennes copies trouvées dans `D:\Documents\sae-project-bot-v1\` (branche `pr-23-music`, une
  copie 1.12 et une copie sans Git). **Je n'y ai pas touché.** Elles sont anciennes (1.11 et 1.12) et
  contiennent des fichiers `.env` : à supprimer quand tu veux, après avoir vérifié qu'il n'y a rien à garder.

## 2. Audit

Ce qui est sain : architecture en couches respectée (services sans discord.py, SQL dans `src/database/`,
cogs minces), migrations uniquement additives avec sauvegarde, secrets masqués, aucune vulnérabilité
connue dans les dépendances épinglées (`pip-audit`), 1096 tests sur `main`.

Ce qui a été trouvé et corrigé :

| Constat | Correction |
|---|---|
| Trois files de notifications (GitLab, GitHub, projets) recopiaient la même boucle ; GitHub et projets dépendaient du module GitLab | Socle commun `src/notifications.py` (`NotificationQueue`) ; `src/gitlab/dispatcher.py` le réexporte |
| Arrêt du bot pendant un envoi : message parti sur Discord mais pas noté envoyé, donc **renvoyé au redémarrage** | Ports fermés d'abord, puis l'envoi en cours peut finir (10 s au plus, systemd en laisse 30) |
| Remise en file au démarrage faite **après** l'ouverture du port : un webhook reçu entre les deux partait deux fois | Remise en file avant l'ouverture du port |
| Erreur passagère de Discord en vérifiant le salon d'un projet : notification abandonnée comme « salon introuvable » | Nouvel essai ; échec durable noté `failed`, plus `skipped` |
| Limite de débit GitHub (B4) affichée comme « accès refusé » | Reconnue (HTTP 429, ou 403 avec `x-ratelimit-remaining: 0`) et présentée comme passagère |
| `src/config.py` (1168 lignes) mélangeait `.env` et fichiers TOML | Paquet `src/config/` : `settings.py`, `files.py`, `base.py` ; tout reste importable depuis `src.config` |
| 4 imports inutiles, 1 variable inutile, 2 `zip` sans `strict` | Corrigés ; `ruff check` (règles F, E9, B) ajouté aux dépendances de dev et à la CI |
| 1.14.0 : un statut **VALIDÉ** était compté comme « terminé » dans `/suivi progression` | VALIDÉ = accepté par l'équipe, pas réalisé ; une question EN COURS reste ouverte (comme dans le DASHBOARD) |

Restent ouverts (hors périmètre ou à décider) : `/suivi fonctionnalites restantes` compte aussi les lignes
ABANDONNÉ ; `LAVALINK_PASSWORD` est encore dans la liste des secrets masqués (sans effet) ; 54 blocs
d'imports non triés et des lignes longues (style seulement, non imposé) ; tout le code de la 1.14.0 et
de la 1.15.0 n'a été testé qu'avec de faux objets Discord.

## 3. Vérification du classeur (`/suivi verifier`)

Le vrai Google Sheet « GAME DESIGN DATABASE — LUNASTEH » a pu être lu (connecteur Google Sheets) : 9 onglets,
titre en ligne 1, en-têtes en ligne 2, listes déroulantes de statuts. Les contrôles sont écrits pour
cette structure (détail des codes dans `docs/suivi-excel.md`) : identifiants, listes, champs
obligatoires, dates, renvois entre onglets (plages « FEAT-001 à FEAT-008 » comprises), éléments
VALIDÉ sans décision dans DECISIONS, compteurs du DASHBOARD recalculés, éléments absents du CHANGELOG.

**Résultat sur le classeur actuel** (transcription des identifiants, statuts, dates et renvois) :
aucune erreur, DASHBOARD exact, CHANGELOG complet, aucun renvoi cassé. **5 avertissements** : FEAT-002,
FEAT-003, FEAT-004, FEAT-007 et FEAT-008 sont VALIDÉ sans décision dans DECISIONS. C'est exactement la
question Q-014 du classeur ; il reste à l'équipe de trancher.

Samae lit le Google Sheet **sans clé d'API** grâce à son lien de partage (export .xlsx). Il ne peut rien y
écrire. Pour l'activer : `TRACKER_GOOGLE_SHEET=<adresse du classeur>` dans `.env`, classeur partagé en
lecture à « Tous les utilisateurs disposant du lien ».

## 4. Synchronisation avec le dépôt GitHub (`/suivi synchroniser`)

J'ai compris « outil de synchronisation au dépôt GitHub » comme : **relier le code du dépôt du jeu au
classeur de suivi**. Samae relit le dépôt (commits de la branche principale, pull requests), repère les
identifiants cités (`FEAT-002` dans un message de commit, un titre de PR ou un nom de branche) et
**propose** les statuts : EN COURS quand du code arrive sur une ligne VALIDÉ, TERMINÉ quand un commit de
la branche principale ou une PR fusionnée dit « termine FEAT-002 » ou « Closes FEAT-002 ». Un admin
valide ou refuse avec `/suivi valider` et `/suivi refuser` ; rien n'est appliqué tout seul, rien n'est
proposé pour une ligne non validée par l'équipe, rien n'est écrit sur GitHub. `/suivi code cle:FEAT-002`
liste le code lié à une ligne. Détail : `docs/synchronisation-github.md`.

Si tu pensais à autre chose (par exemple mettre à jour le bot sur le serveur à partir de GitHub), dis-le :
le moteur de lecture du dépôt est réutilisable.

## 5. À faire par Romain

1. Relire et fusionner la PR #26, puis ouvrir et fusionner la PR de `claude/socle-1.15`
   (https://github.com/ElBastiano7553/sae-project-bot/pull/new/claude/socle-1.15) ; la CI testera 3.11 à 3.13.
2. Sur le VPS : `bash deploy/update.sh` (migrations v15 puis v16, sauvegardes automatiques avant chacune).
3. Dans `.env` (facultatif) : `TRACKER_GOOGLE_SHEET`, `GITHUB_SYNC_REPOSITORY=ElBastiano7553/game-project`,
   `GITHUB_SYNC_TOKEN` en lecture seule si le dépôt est privé, `GITHUB_SYNC_INTERVAL_MINUTES`.
4. Dérouler `docs/test-manuel.md` (sections 1.14.0 et 1.15.0) sur un serveur de test.
5. Trancher Q-014 dans le classeur (décisions pour les 5 fonctionnalités VALIDÉ).

## 6. Fichiers

- Nouveaux : `src/notifications.py`, `src/config/` (remplace `src/config.py`), `src/tracker/checks.py`,
  `src/tracker/google.py`, `src/tracker/verify.py`, `src/github/repo_sync.py`, `src/services/repo_sync.py`,
  `src/database/repo_sync.py`, `docs/synchronisation-github.md`, tests `test_socle.py`,
  `test_tracker_checks.py`, `test_repo_sync.py`, `lunasteh_fixture.py` (structure réelle, contenu inventé).
- Modifiés : les trois files de notifications, `bot.py`, `github_app.py`, `cogs/tracker.py`, `help.py`,
  `services/tracker.py`, `connection.py` (migration v16), `pyproject.toml` (ruff, version 1.15.0),
  `requirements-dev.txt`, CI, `.env.example`, README, `commands.md`, `suivi-excel.md`, `test-manuel.md`,
  `deployment-vps.md`, `architecture.md`.
- Aucune nouvelle dépendance d'exécution ; `ruff` en dépendance de développement seulement.
