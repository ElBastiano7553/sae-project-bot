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
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CONTENT_DIR = PROJECT_ROOT / "content" / "messages"

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TRUE_VALUES = {"1", "true", "yes", "oui", "on"}
_FALSE_VALUES = {"0", "false", "no", "non", "off", ""}
# Longueur minimale du secret des webhooks GitLab : un secret court se devine.
MIN_WEBHOOK_SECRET_LENGTH = 16
# Conservation de l'historique : jamais moins que la durée pendant laquelle GitLab peut renvoyer un événement.
MIN_RETENTION_DAYS = 30
DEFAULT_RETENTION_DAYS = 365
AI_PROVIDERS = ("none", "anthropic", "fake")
DEFAULT_BACKUP_KEEP = 14
DEFAULT_BACKUP_INTERVAL_HOURS = 24
DEFAULT_AI_DAILY_LIMIT = 50
DEFAULT_AI_COOLDOWN_SECONDS = 20
# Message d'accueil d'un nouveau membre : supprimé après ce délai (0 = jamais), une semaine au plus.
DEFAULT_WELCOME_DELETE_HOURS = 24
MAX_WELCOME_DELETE_HOURS = 168
# Variables contenant des secrets : leurs valeurs sont masquées dans tous les journaux.
SECRET_ENV_VARS = ("DISCORD_BOT_TOKEN", "GITLAB_WEBHOOK_SECRET", "GITLAB_ACCESS_TOKEN", "AI_API_KEY", "ALERT_WEBHOOK_URL", "GITHUB_WEBHOOK_SECRET")
# Webhooks GitHub : seul ce dépôt est accepté par défaut (dépôt privé du jeu).
DEFAULT_GITHUB_REPOSITORY = "ElBastiano7553/game-project"
DEFAULT_GITHUB_WEBHOOK_PORT = 8081
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


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
    auto_recreate_deleted: bool = True
    history_retention_days: int = DEFAULT_RETENTION_DAYS
    alerts_channel: str | None = None  # clé de salon (config/server.toml) ; None = alertes désactivées
    # Accueil des nouveaux membres (content/accueil.toml) : demande l'intent « Server Members » sur le portail.
    welcome_on_join: bool = False
    welcome_delete_hours: int = DEFAULT_WELCOME_DELETE_HOURS  # 0 = le message d'accueil reste
    gitlab_webhook_secret: str = field(default="", repr=False)
    gitlab_project_id: int | None = None
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    # Erreur dans les variables GITLAB_* / WEBHOOK_* : la V2 reste désactivée, la V1 démarre quand même.
    gitlab_config_error: str | None = None
    ai_provider: str = "none"  # "none" si l'IA est désactivée ou mal configurée
    ai_api_key: str = field(default="", repr=False)
    ai_model: str | None = None
    ai_daily_limit: int = DEFAULT_AI_DAILY_LIMIT  # demandes par jour, toute l'équipe
    ai_cooldown_seconds: int = DEFAULT_AI_COOLDOWN_SECONDS  # délai entre deux demandes d'un membre
    # Erreur dans les variables AI_* : l'IA reste désactivée, le reste du bot démarre.
    ai_config_error: str | None = None
    gitlab_base_url: str = "https://gitlab.com"
    gitlab_access_token: str = field(default="", repr=False)
    gitlab_api_error: str | None = None
    backup_dir: Path | None = None  # None : <dossier de la base>/backups
    backup_keep: int = DEFAULT_BACKUP_KEEP
    backup_interval_hours: int = DEFAULT_BACKUP_INTERVAL_HOURS  # 0 = pas de sauvegarde automatique
    heartbeat_path: Path | None = None  # fichier de vie lu par le contrôle de santé Docker
    # Webhooks GitHub (dépôt du jeu) : indépendants de GitLab, désactivés sans GITHUB_WEBHOOK_SECRET.
    github_webhook_secret: str = field(default="", repr=False)
    github_repository: str = DEFAULT_GITHUB_REPOSITORY
    github_webhook_host: str = "127.0.0.1"
    github_webhook_port: int = DEFAULT_GITHUB_WEBHOOK_PORT
    # Erreur dans les variables GITHUB_* : GitHub reste désactivé, le reste du bot démarre.
    github_config_error: str | None = None

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider != "none"

    @property
    def gitlab_api_enabled(self) -> bool:
        """API GitLab (lecture seule) : token et identifiant du projet nécessaires."""
        return bool(self.gitlab_access_token and self.gitlab_project_id)

    @property
    def gitlab_webhooks_enabled(self) -> bool:
        """La V2 (webhooks GitLab) n'est active que si un secret valide est défini."""
        return bool(self.gitlab_webhook_secret)

    @property
    def github_webhooks_enabled(self) -> bool:
        """Les webhooks GitHub ne sont actifs que si un secret valide est défini."""
        return bool(self.github_webhook_secret)

    @property
    def github_requested(self) -> bool:
        """Vrai si GitHub est configuré, même de façon invalide (affiché en erreur dans /etat)."""
        return self.github_webhooks_enabled or self.github_config_error is not None

    @property
    def gitlab_requested(self) -> bool:
        """Vrai si la V2 est configurée, même de façon invalide (affiché en erreur dans /etat)."""
        return self.gitlab_webhooks_enabled or self.gitlab_config_error is not None

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
        auto_recreate = _parse_bool(env, "AUTO_RECREATE_DELETED_MESSAGES", True, errors)
        welcome_on_join = _parse_bool(env, "WELCOME_ON_JOIN", False, errors)
        welcome_delete_hours = _parse_int(env, "WELCOME_DELETE_AFTER_HOURS", DEFAULT_WELCOME_DELETE_HOURS, 0, errors)
        if welcome_delete_hours > MAX_WELCOME_DELETE_HOURS:
            errors.append(
                f"WELCOME_DELETE_AFTER_HOURS doit valoir au plus {MAX_WELCOME_DELETE_HOURS} heures "
                f"(valeur actuelle : {welcome_delete_hours})."
            )
        retention_raw = env.get("HISTORY_RETENTION_DAYS", "").strip() or str(DEFAULT_RETENTION_DAYS)
        retention = DEFAULT_RETENTION_DAYS
        if retention_raw.isdigit() and int(retention_raw) >= MIN_RETENTION_DAYS:
            retention = int(retention_raw)
        else:
            errors.append(
                f"HISTORY_RETENTION_DAYS doit être un nombre de jours ≥ {MIN_RETENTION_DAYS} (valeur actuelle : {retention_raw!r})."
            )

        # Les erreurs de la V2 sont rangées à part : elles désactivent la V2 sans empêcher la V1 de démarrer.
        gitlab_errors: list[str] = []
        webhook_secret = env.get("GITLAB_WEBHOOK_SECRET", "").strip()
        if webhook_secret and len(webhook_secret) < MIN_WEBHOOK_SECRET_LENGTH:
            gitlab_errors.append(
                f"GITLAB_WEBHOOK_SECRET est trop court (minimum {MIN_WEBHOOK_SECRET_LENGTH} caractères) : "
                "générez-en un avec la commande indiquée dans .env.example."
            )
        project_raw = env.get("GITLAB_PROJECT_ID", "").strip()
        project_id = None
        if project_raw:
            if project_raw.isdigit():
                project_id = int(project_raw)
            else:
                gitlab_errors.append(
                    "GITLAB_PROJECT_ID doit être l'identifiant numérique du projet "
                    f"(GitLab > page du projet > « Project ID »), valeur actuelle : {project_raw!r}."
                )
        webhook_host = env.get("WEBHOOK_HOST", "").strip() or "127.0.0.1"
        port_raw = env.get("WEBHOOK_PORT", "").strip() or "8080"
        webhook_port = 8080
        if port_raw.isdigit() and 1 <= int(port_raw) <= 65535:
            webhook_port = int(port_raw)
        else:
            gitlab_errors.append(f"WEBHOOK_PORT doit être un nombre entre 1 et 65535 (valeur actuelle : {port_raw!r}).")

        if not webhook_secret:
            gitlab_errors = []  # V2 non demandée : les autres variables GitLab ne comptent pas

        backup_keep = _parse_int(env, "BACKUP_KEEP", DEFAULT_BACKUP_KEEP, 1, errors)
        backup_interval = _parse_int(env, "BACKUP_INTERVAL_HOURS", DEFAULT_BACKUP_INTERVAL_HOURS, 0, errors)

        ai = _parse_ai(env)
        github = _parse_github(env, gitlab_port=webhook_port if webhook_secret and not gitlab_errors else None)
        gitlab_base_url = (env.get("GITLAB_BASE_URL", "").strip() or "https://gitlab.com").rstrip("/")
        gitlab_api_error = None
        if not gitlab_base_url.startswith("https://") or "@" in gitlab_base_url:
            # Le token ne doit jamais partir en clair : l'API GitLab reste désactivée.
            gitlab_api_error = f"GITLAB_BASE_URL doit être une adresse https:// sans identifiants (valeur actuelle : {gitlab_base_url!r})"

        if errors:
            raise ConfigError("Configuration invalide :\n- " + "\n- ".join(errors))

        db_path = resolve_path(env.get("DATABASE_PATH", "").strip() or "data/sae_bot.sqlite3", project_root)
        backup_dir = resolve_path(env.get("BACKUP_DIR", "").strip() or db_path.parent / "backups", project_root)

        return cls(
            discord_token=token,
            guild_id=guild_id,
            database_path=db_path,
            admin_role_name=env.get("ADMIN_ROLE_NAME", "").strip() or None,
            log_level=log_level,
            auto_refresh_on_startup=auto_refresh,
            pin_permanent_messages=pin_messages,
            auto_recreate_deleted=auto_recreate,
            history_retention_days=retention,
            alerts_channel=env.get("ALERTS_CHANNEL", "").strip() or None,
            welcome_on_join=welcome_on_join,
            welcome_delete_hours=welcome_delete_hours,
            gitlab_webhook_secret="" if gitlab_errors else webhook_secret,
            gitlab_config_error=" ; ".join(gitlab_errors) if gitlab_errors else None,
            gitlab_project_id=project_id,
            webhook_host=webhook_host,
            webhook_port=webhook_port,
            **ai,
            gitlab_base_url=gitlab_base_url,
            gitlab_access_token="" if gitlab_api_error else env.get("GITLAB_ACCESS_TOKEN", "").strip(),
            gitlab_api_error=gitlab_api_error,
            backup_dir=backup_dir,
            backup_keep=backup_keep,
            backup_interval_hours=backup_interval,
            heartbeat_path=db_path.parent / "heartbeat",
            **github,
        )


def resolve_path(raw: str | PurePath, project_root: PurePath = PROJECT_ROOT) -> PurePath:
    """Rattache un chemin relatif au dossier du projet ; garde tel quel un chemin ancré.

    ``is_absolute()`` ne suffit pas : sous Windows, ``/srv/sauvegardes`` a une racine
    mais pas de lecteur, donc n'est pas « absolu », et ``project_root / chemin`` lui
    collerait le lecteur du projet (``D:/srv/sauvegardes``). On teste donc l'ancrage
    (lecteur ou racine). Le type de chemin suit ``project_root``, ce qui permet de
    tester la sémantique Windows sous Linux avec ``PureWindowsPath``.
    """
    path = type(project_root)(raw)
    return path if path.anchor else project_root / path


def _parse_github(env: Mapping[str, str], *, gitlab_port: int | None) -> dict:
    """Réglages des webhooks GitHub. Une erreur désactive GitHub (``github_config_error``) sans bloquer le bot.

    ``gitlab_port`` : port déjà utilisé par les webhooks GitLab s'ils sont actifs (les deux serveurs
    ne peuvent pas écouter sur le même port).
    """
    secret = env.get("GITHUB_WEBHOOK_SECRET", "").strip()
    repository = env.get("GITHUB_WEBHOOK_REPOSITORY", "").strip() or DEFAULT_GITHUB_REPOSITORY
    host = env.get("GITHUB_WEBHOOK_HOST", "").strip() or "127.0.0.1"
    port_raw = env.get("GITHUB_WEBHOOK_PORT", "").strip() or str(DEFAULT_GITHUB_WEBHOOK_PORT)
    port = DEFAULT_GITHUB_WEBHOOK_PORT
    errors: list[str] = []
    if secret and len(secret) < MIN_WEBHOOK_SECRET_LENGTH:
        errors.append(
            f"GITHUB_WEBHOOK_SECRET est trop court (minimum {MIN_WEBHOOK_SECRET_LENGTH} caractères) : "
            "générez-en un avec la commande indiquée dans .env.example."
        )
    if not _GITHUB_REPOSITORY.match(repository):
        errors.append(f"GITHUB_WEBHOOK_REPOSITORY doit être de la forme propriétaire/dépôt (valeur actuelle : {repository!r}).")
    if port_raw.isdigit() and 1 <= int(port_raw) <= 65535:
        port = int(port_raw)
    else:
        errors.append(f"GITHUB_WEBHOOK_PORT doit être un nombre entre 1 et 65535 (valeur actuelle : {port_raw!r}).")
    if gitlab_port is not None and port == gitlab_port:
        errors.append(
            f"GITHUB_WEBHOOK_PORT ({port}) est déjà utilisé par les webhooks GitLab (WEBHOOK_PORT) : "
            f"choisissez un autre port, par exemple {DEFAULT_GITHUB_WEBHOOK_PORT}."
        )
    if not secret:
        errors = []  # GitHub non demandé : les autres variables GitHub ne comptent pas
    return {
        "github_webhook_secret": "" if errors else secret,
        "github_repository": repository,
        "github_webhook_host": host,
        "github_webhook_port": port,
        "github_config_error": " ; ".join(errors) if errors else None,
    }


def _parse_ai(env: Mapping[str, str]) -> dict:
    """Réglages de l'IA. Une erreur désactive l'IA (``ai_config_error``) sans bloquer le démarrage."""
    provider = env.get("AI_PROVIDER", "none").strip().lower() or "none"
    key = env.get("AI_API_KEY", "").strip()
    model = env.get("AI_MODEL", "").strip() or None
    problems: list[str] = []
    if provider not in AI_PROVIDERS:
        problems.append(f"AI_PROVIDER doit valoir {', '.join(AI_PROVIDERS)} (valeur actuelle : {provider!r})")
    elif provider == "anthropic" and not key:
        problems.append("AI_API_KEY est vide alors que AI_PROVIDER=anthropic")
    limits = {}
    for name, var, default, minimum in (
        ("ai_daily_limit", "AI_DAILY_LIMIT", DEFAULT_AI_DAILY_LIMIT, 1),
        ("ai_cooldown_seconds", "AI_COOLDOWN_SECONDS", DEFAULT_AI_COOLDOWN_SECONDS, 0),
    ):
        raw = env.get(var, "").strip() or str(default)
        if raw.isdigit() and int(raw) >= minimum:
            limits[name] = int(raw)
        else:
            limits[name] = default
            problems.append(f"{var} doit être un nombre entier ≥ {minimum} (valeur actuelle : {raw!r})")
    if provider == "none":
        problems = []  # IA non demandée : les autres variables AI_* ne comptent pas
    if problems:
        return {"ai_provider": "none", "ai_config_error": " ; ".join(problems), **limits}
    return {"ai_provider": provider, "ai_api_key": key, "ai_model": model, **limits}


def secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Valeurs des secrets définis, pour les masquer dans les journaux (jamais affichées)."""
    env = os.environ if env is None else env
    return [value.strip() for key in SECRET_ENV_VARS if (value := env.get(key, "")).strip()]


def _parse_int(env: Mapping[str, str], key: str, default: int, minimum: int, errors: list[str]) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    if raw.isdigit() and int(raw) >= minimum:
        return int(raw)
    errors.append(f"{key} doit être un nombre entier ≥ {minimum} (valeur actuelle : {raw!r}).")
    return default


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


# --- Structure du serveur (config/server.toml) -------------------------------------------
#
# /admin installer n'en lit que les catégories et salons (noms et anciens noms). /refonte lit
# tout : profils, styles d'affichage, emojis, sujets, types de salons, niveaux
# d'accès (permissions), rôles avec couleurs et politique de réorganisation.

CHANNEL_TYPES = ("text", "voice", "announcement", "forum", "media", "stage")
TEXT_LIKE_TYPES = ("text", "announcement", "forum", "media")
# Permissions qu'un rôle ou un niveau d'accès peut citer, avec leur nom dans Discord.
PERMISSION_LABELS = {
    "view_channel": "Voir les salons",
    "send_messages": "Envoyer des messages",
    "send_messages_in_threads": "Envoyer des messages dans les fils",
    "create_public_threads": "Créer des fils publics",
    "create_private_threads": "Créer des fils privés",
    "embed_links": "Intégrer des liens",
    "attach_files": "Joindre des fichiers",
    "add_reactions": "Ajouter des réactions",
    "use_external_emojis": "Utiliser des emojis externes",
    "read_message_history": "Voir les anciens messages",
    "use_application_commands": "Utiliser les commandes",
    "create_polls": "Créer des sondages",
    "connect": "Se connecter (vocal)",
    "speak": "Parler (vocal)",
    "stream": "Vidéo (vocal)",
    "use_voice_activation": "Détection de la voix",
    "request_to_speak": "Demander la parole",
    "change_nickname": "Changer de pseudo",
    "create_instant_invite": "Créer une invitation",
    "create_events": "Créer des événements",
    "pin_messages": "Épingler des messages",
    # Permissions de modération : réservées aux rôles kind = "staff".
    "manage_messages": "Gérer les messages",
    "manage_threads": "Gérer les fils",
    "manage_events": "Gérer les événements",
    "manage_nicknames": "Gérer les pseudos",
    "moderate_members": "Exclure temporairement",
    "mute_members": "Rendre muet (vocal)",
    "deafen_members": "Mettre en sourdine (vocal)",
    "move_members": "Déplacer des membres (vocal)",
    "kick_members": "Expulser des membres",
    "ban_members": "Bannir des membres",
    "mention_everyone": "Mentionner @everyone",
    "view_audit_log": "Voir le journal d'audit",
    "manage_channels": "Gérer les salons",
    "manage_roles": "Gérer les rôles",
    "manage_webhooks": "Gérer les webhooks",
    "manage_guild": "Gérer le serveur",
}
# Permissions sensibles : seulement pour un rôle de type « staff », jamais sur un rôle ordinaire.
SENSITIVE_PERMISSIONS = frozenset(
    {
        "manage_messages", "manage_threads", "manage_events", "manage_nicknames", "moderate_members", "mute_members",
        "deafen_members", "move_members", "kick_members", "ban_members", "mention_everyone", "view_audit_log",
        "manage_channels", "manage_roles", "manage_webhooks", "manage_guild",
    }
)
# Jamais accordée par Samae, quel que soit le fichier de configuration.
FORBIDDEN_PERMISSIONS = frozenset({"administrator"})
ROLE_KINDS = ("staff", "responsabilite", "fonction", "membre", "notification")
# Seuls ces rôles peuvent être choisis par les membres eux-mêmes (/roles).
SELF_ASSIGNABLE_KINDS = ("fonction", "notification")
# Cibles spéciales des niveaux d'accès (en plus de « role:<clé> ») :
ACCESS_TARGETS = ("@everyone", "@bot", "@admin")


@dataclass(frozen=True)
class CategorySpec:
    key: str
    name: str
    aliases: tuple[str, ...] = ()  # anciens noms : la catégorie est renommée au lieu d'être recréée
    private: bool = False  # réservée au staff (accès « staff » par défaut)
    emoji: str = ""
    access: str = ""  # niveau d'accès ([access.<clé>]) ; vide : « public », ou « staff » si private
    profiles: tuple[str, ...] = ()  # vide : présente dans tous les profils


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    name: str
    category_key: str
    type: str = "text"  # voir CHANNEL_TYPES
    aliases: tuple[str, ...] = ()  # anciens noms reconnus (renommage plutôt que création)
    optional: bool = False  # créé par /refonte seulement si on le demande ; jamais signalé comme manquant
    emoji: str = ""
    topic: str = ""  # sujet affiché sous le nom du salon (salons textuels)
    access: str = ""  # vide : celui de la catégorie
    profiles: tuple[str, ...] = ()
    # Salon de repli pour les messages de Samae tant que ce salon (facultatif) n'existe pas.
    fallback: str = ""


@dataclass(frozen=True)
class RoleSpec:
    """Rôle proposé par /refonte : couleur, place dans la hiérarchie (ordre du fichier) et permissions minimales."""

    key: str
    name: str
    description: str = ""
    color: int | None = None  # 0xRRGGBB ; None : couleur par défaut de Discord
    permissions: tuple[str, ...] = ()  # permissions du serveur, au strict nécessaire
    kind: str = "fonction"  # voir ROLE_KINDS
    hoist: bool = False  # affiché séparément dans la liste des membres
    mentionable: bool = False
    optional: bool = False
    self_assignable: bool = False  # proposé dans le choix des rôles (/roles) : fonction ou notification seulement
    aliases: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccessRule:
    """Permissions d'une cible (@everyone, @bot, @admin ou role:<clé>) dans un niveau d'accès."""

    target: str
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccessSpec:
    key: str
    label: str
    rules: tuple[AccessRule, ...] = ()


@dataclass(frozen=True)
class StyleSpec:
    """Convention de nommage : {emoji} et {nom} ; sans emoji, seul {nom} est gardé."""

    key: str
    label: str
    category: str = "{nom}"
    channel: str = "{nom}"
    voice: str = "{nom}"

    def render(self, spec: CategorySpec | ChannelSpec) -> str:
        if isinstance(spec, CategorySpec):
            pattern = self.category
        else:
            pattern = self.voice if spec.type in ("voice", "stage") else self.channel
        if not spec.emoji or "{emoji}" not in pattern:
            return spec.name
        return pattern.replace("{emoji}", spec.emoji).replace("{nom}", spec.name)


@dataclass(frozen=True)
class ProfileSpec:
    key: str
    label: str
    description: str = ""
    style: str = ""


@dataclass(frozen=True)
class RedesignPolicy:
    """Section [refonte] : comportement par défaut de /refonte."""

    profile: str = "projet"
    style: str = "projet"
    permissions: bool = True  # corriger les permissions des éléments reconnus
    positions: bool = True  # ordonner catégories, salons et rôles selon le fichier
    edit_roles: bool = True  # compléter les rôles existants (couleur, permissions manquantes)
    deletions_allowed: bool = True  # autoriser le mode suppressions:True (toujours sur confirmation séparée)
    keep: tuple[str, ...] = ()  # éléments personnalisés à conserver tels quels (noms)
    obsolete: tuple[str, ...] = ()  # éléments déclarés obsolètes : suppression proposée (jamais automatique)


@dataclass(frozen=True)
class ServerLayout:
    """Structure attendue du serveur (config/server.toml)."""

    categories: dict[str, CategorySpec]
    channels: dict[str, ChannelSpec]
    roles: dict[str, RoleSpec] = field(default_factory=dict)
    access: dict[str, AccessSpec] = field(default_factory=dict)
    styles: dict[str, StyleSpec] = field(default_factory=dict)
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)
    policy: RedesignPolicy = field(default_factory=RedesignPolicy)


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
    filename = path.name
    profiles = _parse_profiles(data, filename)
    styles = _parse_styles(data, filename)
    known_profiles = set(profiles)
    categories: dict[str, CategorySpec] = {}
    for key, value in data.get("categories", {}).items():
        where = f"categories.{key}"
        private = _bool_value(value, "private", where, filename)
        categories[key] = CategorySpec(
            key=key,
            name=_require_str(value, "name", where),
            aliases=_str_list(value, "aliases", where, filename),
            private=private,
            emoji=_emoji(value, where, filename),
            access=str(value.get("access", "staff" if private else "")).strip(),
            profiles=_profile_list(value, where, filename, known_profiles),
        )
    channels: dict[str, ChannelSpec] = {}
    for key, value in data.get("channels", {}).items():
        where = f"channels.{key}"
        category_key = _require_str(value, "category", where)
        if category_key not in categories:
            raise ConfigError(f"{filename} : [{where}] référence une catégorie inconnue « {category_key} ».")
        channel_type = value.get("type", "text")
        if channel_type not in CHANNEL_TYPES:
            raise ConfigError(f"{filename} : [{where}] type doit valoir l'une de ces valeurs : {', '.join(CHANNEL_TYPES)}.")
        name = _require_str(value, "name", where)
        if channel_type in TEXT_LIKE_TYPES and (name != name.lower() or any(ch.isspace() for ch in name) or len(name) > 100):
            # Discord transforme lui-même ces noms : le salon ne correspondrait jamais au nom attendu.
            raise ConfigError(
                f"{filename} : [{where}] le nom d'un salon textuel s'écrit en minuscules, sans espace "
                f"(100 caractères au plus), par exemple « comptes-rendus » (valeur : {name!r})."
            )
        topic = str(value.get("topic", "")).strip()
        if len(topic) > 1024:
            raise ConfigError(f"{filename} : [{where}] le sujet dépasse 1024 caractères (limite de Discord).")
        channels[key] = ChannelSpec(
            key=key,
            name=name,
            category_key=category_key,
            type=channel_type,
            aliases=_str_list(value, "aliases", where, filename),
            optional=_bool_value(value, "optional", where, filename),
            emoji=_emoji(value, where, filename),
            topic=topic,
            access=str(value.get("access", "")).strip(),
            profiles=_profile_list(value, where, filename, known_profiles),
            fallback=str(value.get("fallback", "")).strip(),
        )
    for spec in channels.values():
        if not spec.fallback:
            continue
        target = channels.get(spec.fallback)
        if target is None or spec.fallback == spec.key:
            raise ConfigError(f"{filename} : [channels.{spec.key}] « fallback » doit être la clé d'un autre salon (valeur : {spec.fallback!r}).")
        if target.fallback or {target.type, spec.type} - {"text", "announcement"}:
            raise ConfigError(
                f"{filename} : [channels.{spec.key}] le salon de repli « {spec.fallback} » doit être un salon textuel "
                "sans repli lui-même."
            )
    if not channels:
        raise ConfigError(f"{filename} ne déclare aucun salon.")
    roles = {key: _parse_role(key, value, filename, known_profiles) for key, value in data.get("roles", {}).items()}
    access = _parse_access(data, filename, set(roles), {k for k, r in roles.items() if r.kind == "staff"})
    for spec in (*categories.values(), *channels.values()):
        if spec.access and spec.access not in access:
            kind = "categories" if isinstance(spec, CategorySpec) else "channels"
            raise ConfigError(f"{filename} : [{kind}.{spec.key}] niveau d'accès inconnu « {spec.access} » (voir [access]).")
    policy = _parse_policy(data, filename, profiles, styles)
    layout = ServerLayout(categories, channels, roles, access, styles, profiles, policy)
    for style in styles.values():
        for spec in (*categories.values(), *channels.values()):
            rendered = style.render(spec)
            if len(rendered) > 100:
                raise ConfigError(f"{filename} : avec le style « {style.key} », « {rendered} » dépasse 100 caractères.")
    return layout


def _emoji(table: Mapping, where: str, filename: str) -> str:
    raw = table.get("emoji", "")
    if not isinstance(raw, str) or len(raw.strip()) > 8 or any(ch.isspace() for ch in raw.strip()):
        raise ConfigError(f"{filename} : [{where}] « emoji » doit être un seul emoji Unicode, par exemple \"📢\".")
    return raw.strip()


def _profile_list(table: Mapping, where: str, filename: str, known: set[str]) -> tuple[str, ...]:
    profiles = _str_list(table, "profiles", where, filename)
    unknown = [p for p in profiles if known and p not in known]
    if unknown:
        raise ConfigError(f"{filename} : [{where}] profil inconnu : {', '.join(unknown)} (voir [profiles]).")
    return profiles


def _parse_profiles(data: Mapping, filename: str) -> dict[str, ProfileSpec]:
    result = {}
    for key, value in data.get("profiles", {}).items():
        where = f"profiles.{key}"
        result[key] = ProfileSpec(
            key=key,
            label=_require_str(value, "label", where),
            description=str(value.get("description", "")).strip(),
            style=str(value.get("style", "")).strip(),
        )
    return result


def _parse_styles(data: Mapping, filename: str) -> dict[str, StyleSpec]:
    result = {}
    for key, value in data.get("styles", {}).items():
        where = f"styles.{key}"
        patterns = {}
        for field_name in ("category", "channel", "voice"):
            pattern = str(value.get(field_name, "{nom}"))
            if "{nom}" not in pattern:
                raise ConfigError(f"{filename} : [{where}] « {field_name} » doit contenir {{nom}} (valeur : {pattern!r}).")
            patterns[field_name] = pattern
        if any(ch.isspace() for ch in patterns["channel"]):
            raise ConfigError(f"{filename} : [{where}] « channel » ne peut pas contenir d'espace (salons textuels).")
        result[key] = StyleSpec(key=key, label=_require_str(value, "label", where), **patterns)
    return result


def _parse_role(key: str, value: Mapping, filename: str, known_profiles: set[str]) -> RoleSpec:
    where = f"roles.{key}"
    name = _require_str(value, "name", where)
    if len(name) > 100:
        raise ConfigError(f"{filename} : [{where}] le nom d'un rôle fait au plus 100 caractères.")
    color = value.get("color")
    parsed_color = None
    if color is not None:
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color.strip()):
            raise ConfigError(f"{filename} : [{where}] « color » s'écrit \"#RRGGBB\", par exemple \"#8294A8\".")
        parsed_color = int(color.strip()[1:], 16)
    kind = str(value.get("kind", "fonction")).strip()
    if kind not in ROLE_KINDS:
        raise ConfigError(f"{filename} : [{where}] « kind » doit valoir l'une de ces valeurs : {', '.join(ROLE_KINDS)}.")
    permissions = _permission_list(value, "permissions", where, filename)
    sensitive = sorted(set(permissions) & SENSITIVE_PERMISSIONS)
    if sensitive and kind != "staff":
        raise ConfigError(
            f"{filename} : [{where}] {', '.join(sensitive)} : permission sensible réservée aux rôles kind = \"staff\"."
        )
    self_assignable = _bool_value(value, "self_assignable", where, filename)
    if self_assignable and (kind not in SELF_ASSIGNABLE_KINDS or permissions):
        # Un membre ne peut jamais se donner un rôle de staff, de responsabilité ou porteur de permissions.
        raise ConfigError(
            f"{filename} : [{where}] « self_assignable » n'est permis que pour un rôle kind = \"fonction\" ou "
            "\"notification\" sans aucune permission."
        )
    return RoleSpec(
        key=key,
        name=name,
        description=str(value.get("description", "")).strip(),
        color=parsed_color,
        permissions=permissions,
        kind=kind,
        hoist=_bool_value(value, "hoist", where, filename),
        mentionable=_bool_value(value, "mentionable", where, filename),
        optional=_bool_value(value, "optional", where, filename),
        self_assignable=self_assignable,
        aliases=_str_list(value, "aliases", where, filename),
        profiles=_profile_list(value, where, filename, known_profiles),
    )


def _permission_list(table: Mapping, key: str, where: str, filename: str) -> tuple[str, ...]:
    names = _str_list(table, key, where, filename)
    forbidden = [n for n in names if n in FORBIDDEN_PERMISSIONS]
    if forbidden:
        raise ConfigError(f"{filename} : [{where}] Samae n'accorde jamais la permission « administrator ».")
    unknown = [n for n in names if n not in PERMISSION_LABELS]
    if unknown:
        raise ConfigError(f"{filename} : [{where}] permission inconnue : {', '.join(unknown)}.")
    return names


def _parse_access(data: Mapping, filename: str, role_keys: set[str], staff_keys: set[str] = frozenset()) -> dict[str, AccessSpec]:
    result = {}
    for key, value in data.get("access", {}).items():
        where = f"access.{key}"
        rules = []
        for target, rule in value.get("rules", {}).items():
            if target not in ACCESS_TARGETS and not (target.startswith("role:") and target[5:] in role_keys):
                raise ConfigError(
                    f"{filename} : [{where}.rules] cible inconnue « {target} » : @everyone, @bot, @admin ou role:<clé d'un rôle>."
                )
            allow = _permission_list(rule, "allow", f"{where}.rules.{target}", filename)
            deny = _permission_list(rule, "deny", f"{where}.rules.{target}", filename)
            if set(allow) & set(deny):
                raise ConfigError(f"{filename} : [{where}.rules.{target}] une permission est à la fois accordée et refusée.")
            sensitive = sorted(set(allow) & SENSITIVE_PERMISSIONS)
            if sensitive and target not in ("@bot", "@admin") and not (target.startswith("role:") and target[5:] in staff_keys):
                raise ConfigError(
                    f"{filename} : [{where}.rules.{target}] permission sensible ({', '.join(sensitive)}) réservée à Samae, "
                    "@admin et aux rôles de type « staff »."
                )
            if target == "@bot" and "view_channel" in deny:
                raise ConfigError(f"{filename} : [{where}] Samae ne peut pas se retirer l'accès à un salon.")
            rules.append(AccessRule(target, allow, deny))
        result[key] = AccessSpec(key=key, label=str(value.get("label", key)).strip() or key, rules=tuple(rules))
    return result


def _parse_policy(data: Mapping, filename: str, profiles: dict, styles: dict) -> RedesignPolicy:
    raw = data.get("refonte", {})
    where = "refonte"
    profile = str(raw.get("profile", "projet" if "projet" in profiles or not profiles else next(iter(profiles))))
    style = str(raw.get("style", profiles[profile].style if profile in profiles and profiles[profile].style else "projet"))
    if profiles and profile not in profiles:
        raise ConfigError(f"{filename} : [refonte] profil inconnu « {profile} ».")
    if styles and style not in styles:
        raise ConfigError(f"{filename} : [refonte] style inconnu « {style} ».")
    return RedesignPolicy(
        profile=profile,
        style=style,
        permissions=_bool_value(raw, "permissions", where, filename, default=True),
        positions=_bool_value(raw, "positions", where, filename, default=True),
        edit_roles=_bool_value(raw, "edit_roles", where, filename, default=True),
        deletions_allowed=_bool_value(raw, "deletions_allowed", where, filename, default=True),
        keep=_str_list(raw, "keep", where, filename),
        obsolete=_str_list(raw, "obsolete", where, filename),
    )


def _str_list(table: Mapping, key: str, where: str, filename: str) -> tuple[str, ...]:
    raw = table.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise ConfigError(f"{filename} : [{where}] « {key} » doit être une liste de textes, par exemple [\"ancien-nom\"].")
    return tuple(v.strip() for v in raw)


def _bool_value(table: Mapping, key: str, where: str, filename: str, *, default: bool = False) -> bool:
    raw = table.get(key, default)
    if not isinstance(raw, bool):
        raise ConfigError(f"{filename} : [{where}] « {key} » doit valoir true ou false, sans guillemets.")
    return raw


@dataclass(frozen=True)
class LinkCatalog:
    """Liens de config/links.toml, avec leur libellé et leur caractère obligatoire ou facultatif."""

    urls: dict[str, str]
    labels: dict[str, str] = field(default_factory=dict)
    required: frozenset[str] = frozenset()

    def label(self, key: str) -> str:
        return self.labels.get(key) or key

    def is_required(self, key: str) -> bool:
        return key in self.required

    @property
    def missing(self) -> list[str]:
        """Liens déclarés mais pas encore renseignés, obligatoires d'abord."""
        empty = [key for key, url in self.urls.items() if not url]
        return sorted(empty, key=lambda key: (not self.is_required(key), list(self.urls).index(key)))


def load_links(path: Path = CONFIG_DIR / "links.toml") -> dict[str, str]:
    """Retourne les liens configurés. Les valeurs vides sont conservées (lien « à configurer »)."""
    return load_link_catalog(path).urls


def load_link_catalog(path: Path = CONFIG_DIR / "links.toml") -> LinkCatalog:
    data = load_toml(path)
    links = data.get("links", {})
    result: dict[str, str] = {}
    for key, value in links.items():
        if not isinstance(value, str):
            raise ConfigError(f"{path.name} : le lien « {key} » doit être une chaîne de caractères.")
        value = value.strip()
        if value and not _is_displayable_url(value):
            raise ConfigError(
                f"{path.name} : le lien « {key} » doit être une adresse https:// complète, sans espace "
                f"ni identifiant de connexion (valeur : {value!r})."
            )
        result[key] = value

    labels_raw = data.get("labels", {})
    if not isinstance(labels_raw, dict) or not all(isinstance(v, str) for v in labels_raw.values()):
        raise ConfigError(f"{path.name} : [labels] doit associer chaque lien à un texte.")
    unknown = sorted(set(labels_raw) - set(result))
    if unknown:
        raise ConfigError(f"{path.name} : [labels] cite des liens inconnus : {', '.join(unknown)}.")

    required_raw = data.get("required", [])
    if not isinstance(required_raw, list) or not all(isinstance(v, str) for v in required_raw):
        raise ConfigError(f"{path.name} : « required » doit être une liste de noms de liens, par exemple [\"gitlab_project\"].")
    unknown = sorted(set(required_raw) - set(result))
    if unknown:
        raise ConfigError(f"{path.name} : « required » cite des liens inconnus : {', '.join(unknown)}.")

    return LinkCatalog(
        urls=result,
        labels={k: v.strip() for k, v in labels_raw.items() if v.strip()},
        required=frozenset(required_raw),
    )


def _is_displayable_url(value: str) -> bool:
    """URL publiable dans un message : https, un nom d'hôte, ni espace ni identifiants (``user:mdp@``)."""
    if not value.startswith("https://") or len(value) > 2048 or any(ch.isspace() for ch in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return bool(parts.hostname) and "@" not in parts.netloc


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
