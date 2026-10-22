"""Point d'entrée du bot.

Lancement (depuis le dossier du projet, environnement virtuel activé) :

    python -m src.main
"""

from __future__ import annotations

import logging
import sys

import aiohttp
import discord
from dotenv import load_dotenv

from src.config import PROJECT_ROOT, ConfigError, Settings, load_links, load_planning, load_server_layout
from src.database import Database
from src.discord_bot.bot import SaeBot
from src.logging_setup import setup_logging
from src.services.content import load_message_templates

log = logging.getLogger("sae_bot")


def check_content_files() -> None:
    """Vérifie les fichiers de config/ et content/ avant de se connecter (erreurs claires au démarrage)."""
    layout = load_server_layout()
    load_links()
    load_planning()
    for template in load_message_templates():
        if template.channel_key not in layout.channels:
            raise ConfigError(
                f"content/messages/{template.key}.toml cible le salon « {template.channel_key} », "
                "absent de config/server.toml."
            )


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"\n❌ {exc}\n\nCorrigez le fichier .env puis relancez le bot.", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, PROJECT_ROOT / "logs")
    try:
        check_content_files()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    # Le bot n'utilise pas les salons vocaux : inutile d'avertir que les bibliothèques audio manquent.
    discord.VoiceClient.warn_nacl = False
    discord.VoiceClient.warn_dave = False

    database = Database(settings.database_path)
    bot = SaeBot(settings, database)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error("Connexion refusée : DISCORD_BOT_TOKEN est invalide. Régénérez le token dans le portail développeur.")
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error("Discord refuse un intent privilégié : ce bot n'en a normalement pas besoin, vérifiez le code.")
        return 1
    except aiohttp.ClientConnectorError as exc:
        log.error("Impossible de joindre Discord (%s). Vérifiez votre connexion Internet ou votre pare-feu.", exc)
        return 1
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
