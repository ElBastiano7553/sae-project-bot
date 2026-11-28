"""Fichiers TOML versionnés du dossier ``config/`` : structure du serveur, liens et planning.

Partagés par l'équipe ; aucune dépendance à Discord.
"""


from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from src.config.base import CONFIG_DIR, ConfigError, load_toml

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
