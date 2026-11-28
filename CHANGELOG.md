# Historique des versions

Format : une entrée par version, la plus récente d'abord. Détail de chaque version dans les guides de
`docs/` et dans l'historique Git. « Non déployé » : fusionné ou proposé, pas encore en service.

## 1.15.0 (28/11/2026, branche `claude/socle-1.15`, non fusionnée)

Socle :
- File de notifications commune (`src/notifications.py`) pour GitLab, GitHub et les projets.
- Arrêt propre : l'envoi en cours se termine, plus de notification renvoyée en double au redémarrage.
- Notifications en attente remises en file avant l'ouverture des ports.
- Erreur passagère de Discord sur le salon d'un projet : nouvel essai au lieu d'un abandon.
- Limite de débit GitHub présentée comme passagère, plus comme un refus d'accès.
- `src/config.py` devient le paquet `src/config/` (importations inchangées).
- ruff (règles F, E9, B) dans la CI ; imports et variables inutiles retirés.
- Suivi : VALIDÉ n'est plus compté comme terminé ; les lignes ABANDONNÉ ne sont plus « restantes ».

Outils :
- `/suivi verifier` et `python -m src.tracker.verify` : vérification du classeur (Google Sheet par son
  lien de partage, fichier, ou dernier import). `TRACKER_GOOGLE_SHEET`.
- `/suivi synchroniser` et `/suivi code` : commits et PR GitHub reliés au suivi, propositions de statut
  validées par un admin. `GITHUB_SYNC_REPOSITORY`, `GITHUB_SYNC_TOKEN`, `GITHUB_SYNC_INTERVAL_MINUTES`.
  Migration v16.

Claude Code : skills `samae-project-context`, `samae-game-design`, `samae-discord-ux`,
`samae-quality-assurance`, `async-python-patterns`.

## 1.14.0 (27/11/2026, PR #26, non fusionnée)

Idées (`/idee`), tier lists (`/tierlist`), débats (`/debat`), suivi du classeur Excel (`/suivi`),
assistant `/samae`, sections ajoutées au compte rendu des réunions. Migration v15.

## 1.13.0

Retrait complet de la musique (`/musique`, Lavalink).

## 1.12.0

Installation sur n'importe quel serveur (`/setup`), multi-serveurs ; refonte de la musique.

## 1.11.0

Musique dans les salons vocaux avec Lavalink 4 (retirée en 1.13.0).

## 1.10.0

Connexion des projets GitLab par une application OAuth (étape B5) ; `/projet diagnostic` ; pas de
nouvel essai quand Discord refuse définitivement un envoi.

## 1.9.0

Connexion des projets GitHub par la GitHub App (étape B4), contrôles de sécurité du parcours.

## 1.8.0

Réception commune des événements des projets (étape B3).

## 1.7.0 et 1.7.1

`/projet ajouter` à partir de l'adresse du dépôt ; projets masqués aux membres qui ne voient aucun
de leurs salons.

## 1.6.0

Salons et filtres par projet (étape B2), `/projet modifier`.

## 1.5.0

Socle multi-projets (étape B1), `/projet liste` et `details`, pagination.

## 1.4.0

Commandes regroupées, choix des rôles, message d'accueil, règlement à sa place.

## 1.3.0

Refonte complète de `/refonte` (permissions sensibles réservées au staff, retour arrière).

## 1.2.0 et 1.2.1

Refonte du serveur, centre d'aide, identité visuelle ; hébergement VPS avec systemd ; notifications
GitHub du dépôt du jeu (webhooks signés, port séparé).

## Avant 1.2.0

- Samae 2.0, lot 1 : gestionnaire de réunions (`/reunion`).
- Lots 1 à 4 : embeds et messages permanents fiables, historique GitLab, statistiques, classement,
  tableau de bord, alertes, assistant IA et API GitLab en lecture seule, sauvegardes SQLite, Docker.
- V2 : notifications GitLab par webhooks sécurisés. V1 : messages permanents.
