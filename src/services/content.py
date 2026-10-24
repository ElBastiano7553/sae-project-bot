"""Chargement et rendu des messages permanents.

Le texte des messages vit dans ``content/messages/*.toml`` (hors du code Python).
Le rendu produit un ``MessagePayload`` neutre, indépendant de discord.py :
c'est ``discord_bot.embeds`` qui le convertit en embed Discord.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from src.config import CONTENT_DIR, ConfigError, Deadline, LinkCatalog, Planning, load_toml
from src.services import text_limits as limits
from src.services.design import Color, discord_timestamp, parse_timestamp
from src.services.self_roles import ROLE_PICKER_ID, SelfRoleCatalog

FOOTER_PREFIX = "Message géré par Samae · réf."
# Anciens pieds de message : un message publié avant le renommage du bot reste reconnu.
LEGACY_FOOTER_PREFIXES = ("Message géré par SAE Project Bot · réf.",)
DEFAULT_COLOR = int(Color.INFO)
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
# {{#cle}} : lien vers le salon associé à cette fonction (config/server.toml), toujours à jour du nom réel.
_CHANNEL_PLACEHOLDER = re.compile(r"\{\{\s*#([a-z0-9_]+)\s*\}\}")
_MISSING_LINK = "*lien à configurer*"
MISSING_FIELD_NAME = "🛠️ À compléter"
# Contenus calculés par le bot (clé « dynamic » d'un fichier de content/messages/).
DYNAMIC_KINDS = {"planning", "links", "announcement", "dashboard", "roles"}
# Boutons d'action ([[actions]] d'un fichier de content/messages/) : traités par Samae, ils survivent
# aux redémarrages (identifiant fixe). Seule action connue : ouvrir le choix des rôles.
ACTIONS = {"roles": ROLE_PICKER_ID}
ANNOUNCEMENT_TITLE_MAX = 200
ANNOUNCEMENT_BODY_MAX = 3500


@dataclass(frozen=True)
class FieldTemplate:
    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True)
class ButtonTemplate:
    label: str
    link: str  # clé dans config/links.toml


@dataclass(frozen=True)
class ActionTemplate:
    label: str
    action: str  # clé de ACTIONS


@dataclass(frozen=True)
class MessageTemplate:
    key: str
    channel_key: str
    title: str
    description: str
    color: int = DEFAULT_COLOR
    fields: tuple[FieldTemplate, ...] = ()
    buttons: tuple[ButtonTemplate, ...] = ()
    dynamic: str | None = None  # "planning", "links", "announcement", "dashboard" ou "roles" (voir DYNAMIC_KINDS)
    actions: tuple[ActionTemplate, ...] = ()
    # Anciennes clés (nom du fichier avant un renommage) : le message déjà publié est repris, pas dupliqué.
    previous_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmbedField:
    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True)
class LinkButton:
    label: str
    url: str


@dataclass(frozen=True)
class ActionButton:
    label: str
    custom_id: str


@dataclass(frozen=True)
class Announcement:
    """Annonce publiée depuis Discord avec /admin annonce (stockée en base, jamais inventée)."""

    title: str
    body: str
    author: str = ""
    updated_at: str = ""  # ISO 8601 UTC


@dataclass(frozen=True)
class MessagePayload:
    title: str
    description: str
    color: int
    footer: str
    fields: tuple[EmbedField, ...] = ()
    buttons: tuple[LinkButton, ...] = ()
    # Liens utilisés par le message mais pas encore renseignés : le message est « provisoire ».
    missing_links: tuple[str, ...] = ()
    actions: tuple[ActionButton, ...] = ()

    @property
    def provisional(self) -> bool:
        return bool(self.missing_links)

    def content_hash(self) -> str:
        """Empreinte du contenu : sert à ne modifier un message que s'il a changé."""
        canonical = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def footer_for(key: str) -> str:
    """Pied de message qui permet aussi de retrouver un message si la base est perdue."""
    return f"{FOOTER_PREFIX} {key}"


def footer_markers(keys: tuple[str, ...] | list[str]) -> set[str]:
    """Pieds de message qui désignent l'une de ces clés, avec le nom actuel du bot ou l'ancien."""
    return {f"{prefix} {key}" for key in keys for prefix in (FOOTER_PREFIX, *LEGACY_FOOTER_PREFIXES)}


# ---------------------------------------------------------------------------
# Chargement
# ---------------------------------------------------------------------------


def load_message_templates(directory: Path = CONTENT_DIR) -> list[MessageTemplate]:
    """Charge tous les fichiers ``*.toml`` du dossier, triés par nom."""
    files = sorted(directory.glob("*.toml"))
    if not files:
        raise ConfigError(f"Aucun message permanent trouvé dans {directory}.")
    return [_parse_template(path) for path in files]


def _parse_template(path: Path) -> MessageTemplate:
    data = load_toml(path)
    where = path.name
    try:
        return MessageTemplate(
            key=path.stem,
            channel_key=_text(data, "channel", where),
            title=_text(data, "title", where),
            description=_text(data, "description", where),
            color=_parse_color(data.get("color"), where),
            fields=tuple(
                FieldTemplate(_text(f, "name", where), _text(f, "value", where), bool(f.get("inline", False)))
                for f in data.get("fields", [])
            ),
            buttons=tuple(ButtonTemplate(_text(b, "label", where), _text(b, "link", where)) for b in data.get("buttons", [])),
            dynamic=_parse_dynamic(data.get("dynamic"), where),
            actions=tuple(_parse_action(a, where) for a in data.get("actions", [])),
            previous_keys=tuple(str(k).strip() for k in data.get("previous_keys", []) if str(k).strip()),
        )
    except (TypeError, AttributeError) as exc:
        raise ConfigError(f"{where} : structure invalide ({exc}).") from exc


def _parse_action(table: dict, where: str) -> ActionTemplate:
    action = _text(table, "action", where)
    if action not in ACTIONS:
        raise ConfigError(f"{where} : action inconnue « {action} » (actions possibles : {', '.join(sorted(ACTIONS))}).")
    return ActionTemplate(_text(table, "label", where), action)


def _parse_dynamic(raw: object, where: str) -> str | None:
    if raw is None:
        return None
    if raw not in DYNAMIC_KINDS:
        raise ConfigError(f"{where} : « dynamic » doit valoir {', '.join(sorted(DYNAMIC_KINDS))} (valeur : {raw!r}).")
    return str(raw)


def _text(table: dict, key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} : la clé « {key} » est obligatoire et doit être un texte non vide.")
    return value.strip()


def _parse_color(raw: object, where: str) -> int:
    if raw is None:
        return DEFAULT_COLOR
    if isinstance(raw, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return int(raw[1:], 16)
    raise ConfigError(f"{where} : la couleur doit être au format \"#RRGGBB\" (valeur : {raw!r}).")


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------


def render_message(
    template: MessageTemplate,
    links: dict[str, str] | LinkCatalog,
    planning: Planning,
    today: dt.date | None = None,
    *,
    announcement: Announcement | None = None,
    dashboard: list[EmbedField] | tuple[EmbedField, ...] | None = None,
    channels: Mapping[str, str] | None = None,
    self_roles: SelfRoleCatalog | None = None,
) -> MessagePayload:
    """Construit le contenu final d'un message en respectant les limites de Discord.

    Un lien vide n'est jamais remplacé par une adresse inventée : le texte indique
    « lien à configurer », le bouton est masqué et une section « À compléter » liste
    les liens manquants en distinguant les obligatoires des facultatifs.
    """
    catalog = links if isinstance(links, LinkCatalog) else LinkCatalog(urls=dict(links))
    used: list[str] = []
    title = template.title

    def fill(text: str) -> str:
        return _fill_links(_fill_channels(text, channels, template.key), catalog.urls, template.key, used)

    description = fill(template.description)
    fields = [
        EmbedField(
            limits.truncate(fill(f.name), limits.FIELD_NAME),
            limits.truncate(fill(f.value), limits.FIELD_VALUE),
            f.inline,
        )
        for f in template.fields
    ]
    if template.dynamic == "planning":
        fields.extend(planning_fields(planning, today or dt.date.today()))
    elif template.dynamic == "links":
        fields.extend(link_fields(catalog))
        used.extend(catalog.urls)
    elif template.dynamic == "announcement":
        title, description, extra = announcement_content(template, announcement)
        fields.extend(extra)
    elif template.dynamic == "dashboard":
        # Champs calculés par services.dashboard à partir de l'historique GitLab enregistré.
        fields.extend(dashboard if dashboard is not None else [EmbedField("⏳ Données", "*tableau de bord pas encore calculé*")])
    elif template.dynamic == "roles":
        fields.extend(role_fields(self_roles))
    elif template.dynamic is not None:
        raise ConfigError(f"{template.key}.toml : valeur « dynamic » inconnue : {template.dynamic!r}.")

    buttons = _buttons(template, catalog.urls, used)
    missing = tuple(dict.fromkeys(key for key in used if not catalog.urls.get(key)))
    if missing and template.dynamic != "links":  # le message de liens a déjà sa propre section
        fields.append(missing_links_field(catalog, missing))

    payload = MessagePayload(
        title=limits.truncate(title, limits.EMBED_TITLE),
        description=limits.truncate(description, limits.EMBED_DESCRIPTION),
        color=template.color,
        footer=footer_for(template.key),
        fields=tuple(fields[: limits.EMBED_FIELDS]),
        buttons=buttons,
        missing_links=missing,
        actions=_actions(template, self_roles),
    )
    return _fit_total(payload)


def _fill_channels(text: str, channels: Mapping[str, str] | None, key: str) -> str:
    """Remplace {{#cle}} par la mention du salon (``<#id>``), ou ``#cle`` si la correspondance est inconnue."""

    def replace(match: re.Match[str]) -> str:
        channel_key = match.group(1)
        if channels is None:
            return f"#{channel_key}"
        if channel_key not in channels:
            raise ConfigError(f"{key}.toml : le salon « {channel_key} » n'existe pas dans config/server.toml.")
        return channels[channel_key]

    return _CHANNEL_PLACEHOLDER.sub(replace, text)


def _fill_links(text: str, links: dict[str, str], key: str, used: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        link_key = match.group(1)
        if link_key not in links:
            raise ConfigError(f"{key}.toml : le lien « {link_key} » n'existe pas dans config/links.toml.")
        used.append(link_key)
        return links[link_key] or _MISSING_LINK

    return _PLACEHOLDER.sub(replace, text)


def _buttons(template: MessageTemplate, links: dict[str, str], used: list[str]) -> tuple[LinkButton, ...]:
    buttons: list[LinkButton] = []
    for button in template.buttons:
        if button.link not in links:
            raise ConfigError(f"{template.key}.toml : le bouton « {button.label} » utilise un lien inconnu « {button.link} ».")
        used.append(button.link)
        url = links[button.link]
        if url:  # un lien non configuré masque simplement le bouton
            buttons.append(LinkButton(limits.truncate(button.label, limits.BUTTON_LABEL), url))
    return tuple(buttons[: limits.BUTTONS_PER_MESSAGE])


def _actions(template: MessageTemplate, self_roles: SelfRoleCatalog | None) -> tuple[ActionButton, ...]:
    actions = []
    for action in template.actions:
        if action.action == "roles" and self_roles is not None and not self_roles.roles:
            continue  # aucun rôle à choisir : le bouton ne mènerait nulle part
        actions.append(ActionButton(limits.truncate(action.label, limits.BUTTON_LABEL), ACTIONS[action.action]))
    return tuple(actions[: limits.BUTTONS_PER_MESSAGE])


def role_fields(catalog: SelfRoleCatalog | None) -> list[EmbedField]:
    """Panneau du choix des rôles : chaque rôle proposé et à quoi il sert (rôles réellement présents)."""
    if catalog is None or not catalog.roles:
        return [
            EmbedField(
                "🎭 Rôles à choisir",
                "*Aucun rôle n'est proposé pour le moment.* Un administrateur peut les créer avec `/refonte lancer`, "
                "puis lancer `/admin actualiser`.",
            )
        ]
    fields = []
    for title, roles in catalog.grouped():
        lines = [f"**{role.name}** — {role.description}" for role in roles]
        fields.extend(_chunked_fields(title, lines))
    return fields


def missing_links_field(catalog: LinkCatalog, missing: tuple[str, ...] | list[str]) -> EmbedField:
    ordered = sorted(missing, key=lambda key: not catalog.is_required(key))
    lines = [
        f"• {catalog.label(key)} — {'**obligatoire**' if catalog.is_required(key) else 'facultatif'}" for key in ordered
    ]
    if any(catalog.is_required(key) for key in ordered):
        lines.append("*Version provisoire : renseignez `config/links.toml`, puis `/admin actualiser` met à jour ce message.*")
    else:
        lines.append("*Liens facultatifs : à ajouter dans `config/links.toml` quand ils existeront.*")
    return EmbedField(MISSING_FIELD_NAME, limits.truncate("\n".join(lines), limits.FIELD_VALUE))


def link_fields(catalog: LinkCatalog) -> list[EmbedField]:
    """Message « liens utiles » : liens renseignés cliquables, puis ceux qui restent à compléter."""
    configured = [f"• [{catalog.label(key)}]({url})" for key, url in catalog.urls.items() if url]
    fields: list[EmbedField] = []
    if configured:
        fields.extend(_chunked_fields("🔗 Liens disponibles", configured))
    else:
        fields.append(EmbedField("🔗 Liens disponibles", "*Aucun lien n'est encore renseigné.*"))
    if catalog.missing:
        fields.append(missing_links_field(catalog, catalog.missing))
    return fields


def announcement_content(
    template: MessageTemplate, announcement: Announcement | None
) -> tuple[str, str, list[EmbedField]]:
    """Titre, texte et champs du message d'annonce. Sans annonce enregistrée : texte du fichier TOML."""
    if announcement is None or not announcement.body.strip():
        return template.title, template.description, []
    title = announcement.title.strip() or template.title
    if not title.startswith(("📣", "📢")):
        title = f"📣 {title}"
    details = []
    if announcement.author:
        details.append(f"par **{announcement.author}**")
    moment = parse_timestamp(announcement.updated_at)
    if moment:
        details.append(f"mise à jour {discord_timestamp(moment, 'f')}")
    fields = [EmbedField("ℹ️ Publication", " · ".join(details))] if details else []
    return title, announcement.body.strip(), fields


def planning_fields(planning: Planning, today: dt.date) -> list[EmbedField]:
    """Champs du message de planning. N'affiche que les données de config/planning.toml."""
    upcoming = [d for d in planning.deadlines if d.date >= today]
    past = [d for d in planning.deadlines if d.date < today]
    fields: list[EmbedField] = []

    if upcoming:
        lines = [_deadline_line(d) for d in upcoming]
        fields.extend(_chunked_fields("📌 Prochaines échéances", lines))
    else:
        fields.append(
            EmbedField(
                "📌 Prochaines échéances",
                "Aucune échéance à venir n'est renseignée.\nAjoutez-les dans `config/planning.toml`, puis lancez `/admin actualiser`.",
            )
        )
    if past:
        fields.extend(_chunked_fields("✔️ Échéances passées", [_deadline_line(d) for d in past[-5:]]))
    if planning.next_steps:
        lines = [f"• {s.title}" + (f" — responsable : **{s.owner}**" if s.owner else " — *responsable à désigner*") for s in planning.next_steps]
        fields.extend(_chunked_fields("➡️ Prochaines étapes", lines))
    else:
        fields.append(EmbedField("➡️ Prochaines étapes", "Aucune étape renseignée dans `config/planning.toml`."))
    return fields


def _deadline_line(deadline: Deadline) -> str:
    line = f"**{deadline.date.strftime('%d/%m/%Y')}** — {deadline.title}"
    return f"{line}\n  ↳ {deadline.details}" if deadline.details else line


def _chunked_fields(name: str, lines: list[str]) -> list[EmbedField]:
    chunks = limits.chunk_lines(lines, limits.FIELD_VALUE)
    return [EmbedField(name if i == 0 else f"{name} (suite)", chunk) for i, chunk in enumerate(chunks)]


def payload_length(payload: MessagePayload) -> int:
    """Nombre de caractères comptés par Discord pour la limite globale de 6000."""
    return (
        len(payload.title)
        + len(payload.description)
        + len(payload.footer)
        + sum(len(f.name) + len(f.value) for f in payload.fields)
    )


def _fit_total(payload: MessagePayload) -> MessagePayload:
    """Retire des champs en fin de message si la limite globale de l'embed est dépassée."""
    while payload.fields and payload_length(payload) > limits.EMBED_TOTAL:
        payload = replace(payload, fields=payload.fields[:-1])
    overflow = payload_length(payload) - limits.EMBED_TOTAL
    if overflow > 0:
        payload = replace(payload, description=limits.truncate(payload.description, len(payload.description) - overflow))
    return payload
