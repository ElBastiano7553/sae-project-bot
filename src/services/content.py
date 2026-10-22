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
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from src.config import CONTENT_DIR, ConfigError, Deadline, Planning, load_toml
from src.services import text_limits as limits

FOOTER_PREFIX = "Message géré par SAE Project Bot · réf."
DEFAULT_COLOR = 0x5865F2
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
_MISSING_LINK = "*lien à configurer*"


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
class MessageTemplate:
    key: str
    channel_key: str
    title: str
    description: str
    color: int = DEFAULT_COLOR
    fields: tuple[FieldTemplate, ...] = ()
    buttons: tuple[ButtonTemplate, ...] = ()
    dynamic: str | None = None  # "planning" pour ajouter les échéances


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
class MessagePayload:
    title: str
    description: str
    color: int
    footer: str
    fields: tuple[EmbedField, ...] = ()
    buttons: tuple[LinkButton, ...] = ()

    def content_hash(self) -> str:
        """Empreinte du contenu : sert à ne modifier un message que s'il a changé."""
        canonical = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def footer_for(key: str) -> str:
    """Pied de message qui permet aussi de retrouver un message si la base est perdue."""
    return f"{FOOTER_PREFIX} {key}"


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
            dynamic=data.get("dynamic"),
        )
    except (TypeError, AttributeError) as exc:
        raise ConfigError(f"{where} : structure invalide ({exc}).") from exc


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
    links: dict[str, str],
    planning: Planning,
    today: dt.date | None = None,
) -> MessagePayload:
    """Construit le contenu final d'un message en respectant les limites de Discord."""
    fields = [
        EmbedField(
            limits.truncate(_fill_links(f.name, links, template.key), limits.FIELD_NAME),
            limits.truncate(_fill_links(f.value, links, template.key), limits.FIELD_VALUE),
            f.inline,
        )
        for f in template.fields
    ]
    if template.dynamic == "planning":
        fields.extend(planning_fields(planning, today or dt.date.today()))
    elif template.dynamic is not None:
        raise ConfigError(f"{template.key}.toml : valeur « dynamic » inconnue : {template.dynamic!r}.")

    payload = MessagePayload(
        title=limits.truncate(template.title, limits.EMBED_TITLE),
        description=limits.truncate(_fill_links(template.description, links, template.key), limits.EMBED_DESCRIPTION),
        color=template.color,
        footer=footer_for(template.key),
        fields=tuple(fields[: limits.EMBED_FIELDS]),
        buttons=_buttons(template, links),
    )
    return _fit_total(payload)


def _fill_links(text: str, links: dict[str, str], key: str) -> str:
    def replace(match: re.Match[str]) -> str:
        link_key = match.group(1)
        if link_key not in links:
            raise ConfigError(f"{key}.toml : le lien « {link_key} » n'existe pas dans config/links.toml.")
        return links[link_key] or _MISSING_LINK

    return _PLACEHOLDER.sub(replace, text)


def _buttons(template: MessageTemplate, links: dict[str, str]) -> tuple[LinkButton, ...]:
    buttons: list[LinkButton] = []
    for button in template.buttons:
        if button.link not in links:
            raise ConfigError(f"{template.key}.toml : le bouton « {button.label} » utilise un lien inconnu « {button.link} ».")
        url = links[button.link]
        if url:  # un lien non configuré masque simplement le bouton
            buttons.append(LinkButton(limits.truncate(button.label, limits.BUTTON_LABEL), url))
    return tuple(buttons[: limits.BUTTONS_PER_MESSAGE])


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
                "Aucune échéance à venir n'est renseignée.\nAjoutez-les dans `config/planning.toml`, puis lancez `/refresh`.",
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
