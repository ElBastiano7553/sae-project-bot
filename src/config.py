"""Chargement et validation de la configuration.

Deux sources de configuration distinctes :

* les **variables d'environnement** (fichier ``.env``) pour les secrets et
  les réglages propres à chaque machine ;
* les **fichiers TOML** du dossier ``config/`` pour la structure du serveur,
  les liens et le planning, qui sont versionnés et partagés par l'équipe.

Les fonctions de ce module ne dépendent pas de Discord : elles sont testables
sans token ni connexion réseau.
"""

from __future__ import annotations

import datetime as dt
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CONTENT_DIR = PROJECT_ROOT / "content" / "messages"

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TRUE_VALUES = {"1", "true", "yes", "oui", "on"}
_FALSE_VALUES = {"0", "false", "no", "non", "off", ""}
# Longueur minimale du secret des webhooks GitLab : un secret court se devine.
MIN_WEBHOOK_SECRET_LENGTH = 16


class ConfigError(Exception):
    """Configuration invalide ou incomplète. Le message est destiné à l'utilisateur."""


# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Réglages lus dans les variables d'environnement."""

    discord_token: str = field(repr=False)
    guild_id: int
    database_path: Path
    admin_role_name: str | None = None
    log_level: str = "INFO"
    auto_refresh_on_startup: bool = True
    pin_permanent_messages: bool = True
    gitlab_webhook_secret: str = field(default="", repr=False)
    gitlab_project_id: int | None = None
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    ai_provider: str = "none"

    @property
    def gitlab_webhooks_enabled(self) -> bool:
        """La V2 (webhooks GitLab) n'est active que si un secret est défini."""
        return bool(self.gitlab_webhook_secret)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_token: bool = True,
        project_root: Path = PROJECT_ROOT,
    ) -> Settings:
        """Construit les réglages et lève ``ConfigError`` avec un message clair si besoin."""
        env = os.environ if env is None else env
        errors: list[str] = []

        token = env.get("DISCORD_BOT_TOKEN", "").strip()
        if require_token and not token:
            errors.append(
                "DISCORD_BOT_TOKEN est vide : copiez le token du bot depuis le portail "
                "développeur Discord dans votre fichier .env."
            )

        guild_raw = env.get("DISCORD_GUILD_ID", "").strip()
        guild_id = 0
        if not guild_raw:
            errors.append("DISCORD_GUILD_ID est vide : indiquez l'identifiant du serveur Discord.")
        elif not guild_raw.isdigit():
            errors.append(
                f"DISCORD_GUILD_ID doit contenir uniquement des chiffres (valeur actuelle : {guild_raw!r})."
            )
        else:
            guild_id = int(guild_raw)

        log_level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in _VALID_LOG_LEVELS:
            errors.append(f"LOG_LEVEL doit valoir l'une de ces valeurs : {', '.join(sorted(_VALID_LOG_LEVELS))}.")

        auto_refresh = _parse_bool(env, "AUTO_REFRESH_ON_STARTUP", True, errors)
        pin_messages = _parse_bool(env, "PIN_PERMANENT_MESSAGES", True, errors)

        webhook_secret = env.get("GITLAB_WEBHOOK_SECRET", "").strip()
        if webhook_secret and len(webhook_secret) < MIN_WEBHOOK_SECRET_LENGTH:
            errors.append(
                f"GITLAB_WEBHOOK_SECRET est trop court (minimum {MIN_WEBHOOK_SECRET_LENGTH} caractères) : "
                "générez-en un avec la commande indiquée dans .env.example."
            )
        project_raw = env.get("GITLAB_PROJECT_ID", "").strip()
        project_id = None
        if project_raw:
            if project_raw.isdigit():
                project_id = int(project_raw)
            else:
                errors.append(
                    "GITLAB_PROJECT_ID doit être l'identifiant numérique du projet "
                    f"(GitLab > page du projet > « Project ID »), valeur actuelle : {project_raw!r}."
                )
        webhook_host = env.get("WEBHOOK_HOST", "").strip() or "127.0.0.1"
        port_raw = env.get("WEBHOOK_PORT", "").strip() or "8080"
        webhook_port = 8080
        if port_raw.isdigit() and 1 <= int(port_raw) <= 65535:
            webhook_port = int(port_raw)
        else:
            errors.append(f"WEBHOOK_PORT doit être un nombre entre 1 et 65535 (valeur actuelle : {port_raw!r}).")

        if errors:
            raise ConfigError("Configuration invalide :\n- " + "\n- ".join(errors))

        db_path = Path(env.get("DATABASE_PATH", "").strip() or "data/sae_bot.sqlite3")
        if not db_path.is_absolute():
            db_path = project_root / db_path

        return cls(
            discord_token=token,
            guild_id=guild_id,
            database_path=db_path,
            admin_role_name=env.get("ADMIN_ROLE_NAME", "").strip() or None,
            log_level=log_level,
            auto_refresh_on_startup=auto_refresh,
            pin_permanent_messages=pin_messages,
            gitlab_webhook_secret=webhook_secret,
            gitlab_project_id=project_id,
            webhook_host=webhook_host,
            webhook_port=webhook_port,
            ai_provider=env.get("AI_PROVIDER", "none").strip().lower() or "none",
        )


def _parse_bool(env: Mapping[str, str], key: str, default: bool, errors: list[str]) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    errors.append(f"{key} doit valoir true ou false (valeur actuelle : {raw!r}).")
    return default


# ---------------------------------------------------------------------------
# Fichiers TOML : structure du serveur, liens, planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategorySpec:
    key: str
    name: str


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    name: str
    category_key: str
    type: str = "text"  # "text" ou "voice"


@dataclass(frozen=True)
class ServerLayout:
    """Structure attendue du serveur (config/server.toml)."""

    categories: dict[str, CategorySpec]
    channels: dict[str, ChannelSpec]


@dataclass(frozen=True)
class Deadline:
    date: dt.date
    title: str
    details: str = ""


@dataclass(frozen=True)
class NextStep:
    title: str
    owner: str = ""


@dataclass(frozen=True)
class Planning:
    deadlines: tuple[Deadline, ...] = ()
    next_steps: tuple[NextStep, ...] = ()


def load_toml(path: Path) -> dict:
    """Lit un fichier TOML et transforme les erreurs en ``ConfigError`` lisibles."""
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Fichier de configuration introuvable : {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Erreur de syntaxe dans {path.name} : {exc}") from exc


def load_server_layout(path: Path = CONFIG_DIR / "server.toml") -> ServerLayout:
    data = load_toml(path)
    categories = {
        key: CategorySpec(key=key, name=_require_str(value, "name", f"categories.{key}"))
        for key, value in data.get("categories", {}).items()
    }
    channels: dict[str, ChannelSpec] = {}
    for key, value in data.get("channels", {}).items():
        where = f"channels.{key}"
        category_key = _require_str(value, "category", where)
        if category_key not in categories:
            raise ConfigError(f"{path.name} : [{where}] référence une catégorie inconnue « {category_key} ».")
        channel_type = value.get("type", "text")
        if channel_type not in {"text", "voice"}:
            raise ConfigError(f"{path.name} : [{where}] type doit valoir \"text\" ou \"voice\".")
        channels[key] = ChannelSpec(
            key=key,
            name=_require_str(value, "name", where),
            category_key=category_key,
            type=channel_type,
        )
    if not channels:
        raise ConfigError(f"{path.name} ne déclare aucun salon.")
    return ServerLayout(categories=categories, channels=channels)


def load_links(path: Path = CONFIG_DIR / "links.toml") -> dict[str, str]:
    """Retourne les liens configurés. Les valeurs vides sont conservées (lien « à configurer »)."""
    links = load_toml(path).get("links", {})
    result: dict[str, str] = {}
    for key, value in links.items():
        if not isinstance(value, str):
            raise ConfigError(f"{path.name} : le lien « {key} » doit être une chaîne de caractères.")
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ConfigError(f"{path.name} : le lien « {key} » doit commencer par https:// (valeur : {value!r}).")
        result[key] = value
    return result


def load_planning(path: Path = CONFIG_DIR / "planning.toml") -> Planning:
    data = load_toml(path)
    deadlines: list[Deadline] = []
    for index, item in enumerate(data.get("deadlines", []), start=1):
        date = item.get("date")
        if isinstance(date, dt.datetime):
            date = date.date()
        if not isinstance(date, dt.date):
            raise ConfigError(
                f"{path.name} : l'échéance n°{index} doit avoir une date au format AAAA-MM-JJ, sans guillemets."
            )
        deadlines.append(
            Deadline(
                date=date,
                title=_require_str(item, "title", f"deadlines n°{index}"),
                details=str(item.get("details", "")).strip(),
            )
        )
    steps = [
        NextStep(title=_require_str(item, "title", f"next_steps n°{index}"), owner=str(item.get("owner", "")).strip())
        for index, item in enumerate(data.get("next_steps", []), start=1)
    ]
    deadlines.sort(key=lambda d: d.date)
    return Planning(deadlines=tuple(deadlines), next_steps=tuple(steps))


def _require_str(table: Mapping, key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{where}] : la clé « {key} » est obligatoire et doit être un texte non vide.")
    return value.strip()
