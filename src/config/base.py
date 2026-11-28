"""Chemins du projet et erreur de configuration, communs aux deux sources de configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
CONTENT_DIR = PROJECT_ROOT / "content" / "messages"


class ConfigError(Exception):
    """Configuration invalide ou incomplète. Le message est destiné à l'utilisateur."""


def load_toml(path: Path) -> dict:
    """Lit un fichier TOML et transforme les erreurs en ``ConfigError`` lisibles."""
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Fichier de configuration introuvable : {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Erreur de syntaxe dans {path.name} : {exc}") from exc
