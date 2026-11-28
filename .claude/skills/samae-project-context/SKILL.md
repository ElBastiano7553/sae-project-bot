---
name: samae-project-context
description: Architecture, conventions, décisions techniques et règles de l'équipe pour Samae, le bot Discord Python (discord.py) de l'équipe SAE EPITA. À lire AVANT toute modification importante du dépôt sae-project-bot (nouvelle commande, base SQLite, notifications, suivi du projet, déploiement).
---

# Samae : contexte du projet

Samae est le bot Discord de l'équipe SAE EPITA (5 membres, jeu « LUNASTEH »). Tout est en **français** :
commentaires, messages Discord, documentation, commits. Référence détaillée : `ARCHITECTURE.md`,
`DEVELOPMENT.md` et `docs/`. Les autres skills du dépôt : `samae-game-design` (classeur de suivi),
`samae-discord-ux` (interfaces), `samae-quality-assurance` (validation de chaque changement).

## Pile

- Python 3.11 à 3.13 (CI sur les trois), **discord.py 2.7.1** (pas Pycord), aiohttp, SQLite (WAL),
  openpyxl, SDK Anthropic facultatif. Versions épinglées dans `requirements.txt`.
- Lancer : `python -m src.main`. Tests : `python -m pytest -q`. Lint : `python -m ruff check src tests`.
- Hébergement : VPS Linux, service systemd (`deploy/`), mise à jour `bash deploy/update.sh`, retour
  arrière `bash deploy/rollback.sh`. Romain fusionne et déploie lui-même.

## Architecture en couches (à respecter, pas à réorganiser)

| Couche | Où | Règle |
|---|---|---|
| Configuration | `src/config/` (`settings.py` = `.env`, `files.py` = TOML de `config/`, `base.py` = chemins, `ConfigError`) | Tout réglage passe par `Settings.from_env` ; secret ⇒ `field(repr=False)` + `SECRET_ENV_VARS` |
| Métier | `src/services/<domaine>.py` | **Aucun import de discord.py** ; permissions décidées ici à partir d'un `Actor(user_id, name, is_admin, guild_id)` |
| Données | `src/database/<domaine>.py` | Seul endroit avec du SQL ; écritures concurrentes vérifiées dans la transaction |
| Migrations | `MIGRATIONS` dans `src/database/connection.py` | **Ajouts seulement**, à la fin (v16 en 1.15.0) ; sauvegarde automatique avant |
| Intégrations | `src/gitlab/`, `src/github/`, `src/providers/`, `src/tracker/`, `src/ai/` | Sans discord.py ; erreurs réseau traduites en exceptions au message français |
| Notifications | `src/notifications.py` (`NotificationQueue`, `send_with_retries`, statuts) | Les trois files (GitLab, GitHub, projets) en héritent ; arrêt propre via `stop()` |
| Discord | `src/discord_bot/bot.py` (cycle de vie), `cogs/<domaine>.py` (adaptateurs minces), `<domaine>_ui.py` (embeds, vues) | Identifier la personne, appeler le service, répondre |

Une intégration externe en panne ne doit jamais arrêter le bot : chaque démarrage d'intégration est
protégé (`start_*` dans `bot.py`) et son erreur apparaît dans `/etat`.

## Conventions

- Aide : chaque commande a une `HelpEntry` dans `src/services/help.py` (un test l'impose) ; une
  sous-commande d'admin d'un groupe public porte `extras={"admin": True}` et `@admin_only()`, et figure
  dans `ADMIN_SUBCOMMANDS` de `tests/test_discord_commands.py`.
- Espace d'équipe : groupes listés dans `HOME_ONLY_COMMANDS` (`bot.py`), cog avec `interaction_check`
  = `require_team_space`.
- Pas plus de 25 sous-commandes par groupe (limite Discord ; `/suivi` en a 19).
- Journaux : `logging.getLogger(__name__)`, jamais de secret ; les actions d'admin passent par
  `bot.record_admin_action`.

## Règles de l'équipe (impératives)

- Une **décision officielle** ou un changement du suivi n'est jamais appliqué sans validation humaine
  (bouton de confirmation d'un admin). Les propositions automatiques (synchronisation GitHub) restent
  en attente.
- Le classeur Excel ou Google Sheet n'est **jamais** modifié : lecture, import, export dans un nouveau fichier.
- Jamais de secret dans Git, les journaux ou Discord ; tokens seulement dans `.env` sur le serveur.
- Aucune opération destructive (suppression de données, reset de base, migration destructive).
- Aucun service payant engagé : l'IA (`AI_PROVIDER`) est facultative, tout doit marcher sans.
- Aucun intent privilégié (sauf « membres » si `WELCOME_ON_JOIN`) ; `AllowedMentions.none()` partout.
- Travail sur une branche dédiée, PR vers `main`.

## Documentation à tenir à jour

README (« État actuel »), `CHANGELOG.md`, `docs/commands.md`, `docs/test-manuel.md`, `.env.example`,
`ARCHITECTURE.md` si une couche change, et le guide du domaine (`docs/suivi-excel.md`,
`docs/synchronisation-github.md`, `docs/meeting-manager.md`…).
