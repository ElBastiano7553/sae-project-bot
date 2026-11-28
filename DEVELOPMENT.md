# Développer Samae

Guide pour reprendre le code. Architecture : [ARCHITECTURE.md](ARCHITECTURE.md). Règles du projet pour
Claude Code : skills `samae-*` dans `.claude/skills/` ([docs/skills.md](docs/skills.md)).

## Prérequis

- Python 3.11, 3.12 ou 3.13 (3.12 recommandé ; la CI teste les trois).
- Git. Un bot Discord **de test** et un serveur de test pour essayer sur Discord (jamais le bot de l'équipe).
- Facultatif : Docker (l'image est vérifiée par la CI).

## Installer (Windows, PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Dans `.env`, seul `DISCORD_BOT_TOKEN` est obligatoire (token du bot de test). Le fichier n'est jamais
versionné (`.gitignore`). Linux : `python3 -m venv .venv && . .venv/bin/activate`.

## Lancer

```powershell
python -m src.main
```

Sans configuration, le bot s'arrête avec un message clair (code 2). Journaux dans `logs/bot.log`, base
dans `data/sae_bot.sqlite3` (sauvegarde automatique avant chaque migration dans `data/backups/`).

## Vérifier avant chaque commit

```powershell
python -m ruff check src tests
python -m pytest -q
```

Les tests n'utilisent ni token, ni réseau, ni vrai Discord : faux Discord (`tests/test_meeting_commands.py`),
faux serveurs GitHub, GitLab et Google (`aiohttp.test_utils.TestServer`), base SQLite temporaire
(fixture `db`). Détail du processus : skill `samae-quality-assurance`.

Outils manuels :

- `python -m src.gitlab.demo push` et `python -m src.github.demo ping` : faux webhooks vers le bot lancé.
- `python -m src.tracker.inspect fichier.xlsx` : structure d'un classeur.
- `python -m src.tracker.verify --google <adresse>` : vérification du classeur (codes 0, 1, 2).

## Ajouter une commande

1. Logique dans `src/services/<domaine>.py` (sans discord.py), permissions avec `Actor`.
2. SQL éventuel dans `src/database/<domaine>.py` ; nouvelle table = migration ajoutée à la fin de
   `MIGRATIONS` (création seulement) + test.
3. Commande dans `src/discord_bot/cogs/<domaine>.py` : `defer` si c'est long, réponse éphémère,
   `send_error` pour les refus ; `extras={"admin": True}` + `@admin_only()` pour une action d'admin.
4. `HelpEntry` dans `src/services/help.py` ; sous-commande d'admin ajoutée à `ADMIN_SUBCOMMANDS`
   dans `tests/test_discord_commands.py`.
5. Tests (service, commande avec faux Discord, refus d'un non-admin).
6. Documentation : `docs/commands.md`, `CHANGELOG.md`, `docs/test-manuel.md`, `.env.example` si besoin.

Conventions d'interface : skill `samae-discord-ux`. Classeur de suivi : skill `samae-game-design`.

## Ajouter un réglage

Dans `src/config/settings.py` : champ de `Settings` (secret ⇒ `field(default="", repr=False)` et nom
ajouté à `SECRET_ENV_VARS`), lecture et validation dans `from_env` (message d'erreur qui dit quoi
écrire), ligne commentée dans `.env.example`, test de `Settings.from_env`.

## Git

- Une branche par sujet, partie de `main` (ou de la branche dont elle dépend, à dire dans la PR).
- Messages de commit en français, à l'impératif ou au nominatif (« Synchronisation du suivi… »).
- PR vers `main` ; la CI (ruff, pytest 3.11 à 3.13, Docker) doit être verte ; Romain fusionne.

## Déployer

Sur le VPS seulement, par Romain : `bash deploy/update.sh` (sauvegarde vérifiée, dépendances,
redémarrage, contrôle), retour arrière `bash deploy/rollback.sh`. Guide : [docs/deployment-vps.md](docs/deployment-vps.md).
