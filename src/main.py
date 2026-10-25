"""Point d'entrée du bot.

Lancement (depuis le dossier du projet, environnement virtuel activé) :

    python -m src.main

Codes de sortie (utilisés par le service systemd, voir deploy/samae.service.in) :

* 0 : arrêt demandé (Ctrl+C, ``systemctl stop``) ;
* 1 : erreur passagère (réseau, erreur inattendue) : systemd relance le bot ;
* 2 : configuration à corriger (.env, fichiers de config/, token refusé, base inutilisable) :
  relancer ne servirait à rien, systemd ne relance pas ;
* 3 : une autre instance tourne déjà sur la même base : systemd ne relance pas.
"""

from __future__ import annotations

import logging
import sqlite3
import sys

import aiohttp
import discord
from dotenv import load_dotenv

from src.config import (
    PROJECT_ROOT,
    ConfigError,
    Settings,
    load_link_catalog,
    load_planning,
    load_server_layout,
    secret_values,
)
from src.database import Database
from src.discord_bot.bot import SaeBot
from src.instance_lock import LOCK_NAME, AlreadyRunning, InstanceLock
from src.gitlab.rules import load_gitlab_rules
from src.logging_setup import setup_logging
from src.services.backup import BackupError
from src.services.content import load_message_templates
from src.services.monitoring import RecentErrors
from src.services.welcome import load_welcome_template

log = logging.getLogger("sae_bot")

EXIT_OK, EXIT_RETRY, EXIT_CONFIG, EXIT_ALREADY_RUNNING = 0, 1, 2, 3


def check_content_files(*, gitlab_enabled: bool = False, welcome_enabled: bool = False) -> None:
    """Vérifie les fichiers de config/ et content/ avant de se connecter (erreurs claires au démarrage).

    Une erreur dans config/gitlab.toml est seulement signalée : elle désactive la V2, pas le bot.
    """
    layout = load_server_layout()
    if gitlab_enabled:
        try:
            load_gitlab_rules(channel_keys=layout.channels.keys())
        except ConfigError as exc:
            log.error("Webhooks GitLab désactivés : %s", exc)
    load_link_catalog()
    load_planning()
    templates = load_message_templates()
    if welcome_enabled:
        templates.append(load_welcome_template())
    for template in templates:
        if template.channel_key not in layout.channels:
            raise ConfigError(
                f"{template.key}.toml cible le salon « {template.channel_key} », absent de config/server.toml."
            )


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"\n❌ {exc}\n\nCorrigez le fichier .env puis relancez le bot.", file=sys.stderr)
        return EXIT_CONFIG

    errors = RecentErrors()
    setup_logging(settings.log_level, PROJECT_ROOT / "logs", secrets=secret_values(), errors=errors)
    log.info("Démarrage : token Discord présent (obligatoire) ; fonctions facultatives :")
    for name, state in settings.feature_summary():
        log.info("  - %s : %s", name, state)
    if settings.gitlab_config_error:
        log.error("Webhooks GitLab désactivés, configuration invalide : %s", settings.gitlab_config_error)
    if settings.github_config_error:
        log.error("Webhooks GitHub désactivés, configuration invalide : %s", settings.github_config_error)
    try:
        check_content_files(gitlab_enabled=settings.gitlab_webhooks_enabled, welcome_enabled=settings.welcome_on_join)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG

    try:  # dossier de la base (data/ par défaut) : créé s'il manque, sur une nouvelle machine
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("Dossier de la base impossible à créer (%s) : %s", settings.database_path.parent, exc.strerror)
        return EXIT_CONFIG
    lock = InstanceLock(settings.database_path.parent / LOCK_NAME)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        log.error("Démarrage annulé : %s", exc)
        return EXIT_ALREADY_RUNNING
    except OSError as exc:
        log.error("Dossier de la base inaccessible (%s) : %s", settings.database_path.parent, exc.strerror)
        return EXIT_CONFIG
    try:
        return _run(settings, errors)
    finally:
        lock.release()


def _run(settings: Settings, errors: RecentErrors) -> int:
    # Le bot n'envoie jamais de son lui-même (la musique passe par Lavalink, qui gère aussi le chiffrement DAVE) :
    # inutile d'avertir que les bibliothèques audio de discord.py (PyNaCl, davey) manquent.
    discord.VoiceClient.warn_nacl = False
    discord.VoiceClient.warn_dave = False

    try:
        database = Database(settings.database_path, backup_dir=settings.backup_dir)
    except BackupError as exc:
        log.error("Migration annulée, base inchangée : %s. Libérez de la place ou vérifiez BACKUP_DIR.", exc)
        return EXIT_CONFIG
    except (RuntimeError, sqlite3.Error) as exc:
        log.error("Base de données inutilisable (%s) : %s", settings.database_path, exc)
        return EXIT_CONFIG
    database.integrity_ok()
    bot = SaeBot(settings, database, errors=errors)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error("Connexion refusée : DISCORD_BOT_TOKEN est invalide. Régénérez le token dans le portail développeur.")
        return EXIT_CONFIG
    except discord.PrivilegedIntentsRequired:
        if settings.welcome_on_join:
            log.error(
                "Discord refuse l'intent « Server Members », nécessaire à WELCOME_ON_JOIN=true. Activez-le dans le portail "
                "développeur (Bot > Privileged Gateway Intents > Server Members Intent) ou mettez WELCOME_ON_JOIN=false."
            )
        else:
            log.error("Discord refuse un intent privilégié : ce bot n'en a normalement pas besoin, vérifiez le code.")
        return EXIT_CONFIG
    except aiohttp.ClientConnectorError as exc:
        log.error("Impossible de joindre Discord (%s). Vérifiez votre connexion Internet ou votre pare-feu.", exc)
        return EXIT_RETRY
    except Exception:
        log.exception("Arrêt sur une erreur inattendue")
        return EXIT_RETRY
    finally:
        database.close()
    log.info("Bot arrêté proprement")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
