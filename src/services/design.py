"""Identité visuelle commune à tous les messages du bot (sans dépendance à discord.py).

Toutes les couleurs, icônes et petites aides de mise en forme passent par ce
module : les embeds des commandes, des messages permanents et des notifications
GitLab restent ainsi cohérents. ``discord_bot.embeds`` s'en sert pour construire
les embeds, ``gitlab.events`` pour choisir la couleur d'une notification.

Identité de Samae : sobre, inspirée des outils de travail collaboratif. Discord ne
laisse choisir que la couleur de la barre latérale d'un embed (le fond et le texte
suivent le thème de chaque lecteur) : la palette de référence sert donc d'accent
et de base aux couleurs d'état, désaturées pour rester lisibles en thème sombre
comme en thème clair.

Palette de référence : fond ``#20242B``, surface ``#2B313B``, accent ``#8294A8``,
texte ``#E6EAF0``.

Règle des couleurs :

* accent (gris-bleu) : information, aide, réunion prévue ;
* vert : réussite, réunion terminée ; rouge : échec ; ambre : avertissement ;
* bleu vif : réunion en cours ; violet : fusion d'une merge request ;
* gris : événement neutre (fermeture, annulation).

Emojis : réservés aux indicateurs d'état utiles dans une liste (réussi, échec,
attention) et aux types d'événements GitLab ; les titres et les boutons s'en passent.
"""

from __future__ import annotations

import datetime as dt
from enum import IntEnum

from src.services import text_limits as limits

BOT_NAME = "Samae"


class Palette(IntEnum):
    """Palette de référence de Samae (seul l'accent est visible dans Discord : barre des embeds)."""

    BACKGROUND = 0x20242B
    SURFACE = 0x2B313B
    ACCENT = 0x8294A8
    TEXT = 0xE6EAF0


class Color(IntEnum):
    SUCCESS = 0x5FA37A  # vert désaturé
    FAILURE = 0xC9565A  # rouge désaturé
    WARNING = 0xD19A4B  # ambre
    INFO = 0x8294A8  # accent de Samae
    MERGED = 0x8C7FC0  # violet désaturé
    NEUTRAL = 0x5E6672  # gris, entre la surface et l'accent
    LIVE = 0x6FA8DC  # bleu vif : réunion en cours


class Tone:
    """Ton d'un message : détermine sa couleur et son icône par défaut."""

    SUCCESS = "success"
    FAILURE = "failure"
    WARNING = "warning"
    INFO = "info"
    NEUTRAL = "neutral"


TONE_COLORS = {
    Tone.SUCCESS: Color.SUCCESS,
    Tone.FAILURE: Color.FAILURE,
    Tone.WARNING: Color.WARNING,
    Tone.INFO: Color.INFO,
    Tone.NEUTRAL: Color.NEUTRAL,
}

TONE_ICONS = {
    Tone.SUCCESS: "✅",
    Tone.FAILURE: "❌",
    Tone.WARNING: "⚠️",
    Tone.INFO: "ℹ️",
    Tone.NEUTRAL: "▫️",
}

# Icônes des catégories de contenu, réutilisées par /help, /project et les notifications.
ICONS = {
    "push": "📤",
    "branch_new": "🌱",
    "branch_deleted": "🗑️",
    "merge_request": "🔀",
    "pipeline": "🛠️",
    "link": "🔗",
    "missing": "🛠️",
    "status": "📊",
    "help": "",
    "admin": "",
    "project": "🎮",
    "history": "🕘",
    "announce": "📣",
    "message": "📌",
    "meeting": "",
    "report": "",
}

SEPARATOR = " · "
MISSING_VALUE = "—"


def tone_color(tone: str) -> int:
    return int(TONE_COLORS.get(tone, Color.INFO))


def footer(*parts: str) -> str:
    """Pied de page standard : « Samae · partie · partie »."""
    text = SEPARATOR.join([BOT_NAME, *[p for p in parts if p]])
    return limits.truncate(text, limits.EMBED_FOOTER)


def discord_timestamp(moment: dt.datetime, style: str = "f") -> str:
    """Horodatage Discord (affiché dans le fuseau et la langue de chaque lecteur).

    Styles utiles : ``f`` (date et heure), ``R`` (relatif : « il y a 3 minutes »), ``d`` (date).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return f"<t:{int(moment.timestamp())}:{style}>"


def parse_timestamp(value: object) -> dt.datetime | None:
    """Lit une date ISO 8601 (GitLab) ou « AAAA-MM-JJ HH:MM:SS » (SQLite, UTC). ``None`` si illisible."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(" UTC"):  # format de certains webhooks GitLab : « 2026-10-22 15:00:00 UTC »
        text = text[:-4]
    text = text.replace("Z", "+00:00")
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


def relative_time(value: object) -> str:
    """Horodatage relatif Discord à partir d'une date texte, ou « date inconnue »."""
    moment = parse_timestamp(value)
    return discord_timestamp(moment, "R") if moment else "date inconnue"


def escape_markdown(text: str) -> str:
    """Neutralise la mise en forme Discord d'un texte brut (titres GitLab, noms…)."""
    for char in "\\*_~|`>":
        text = text.replace(char, "\\" + char)
    return text


def md_link(label: str, url: str | None) -> str:
    """Lien Markdown cliquable ; sans URL, le libellé seul. ``url`` doit déjà être validée."""
    label = escape_markdown(label.replace("[", "(").replace("]", ")"))
    return f"[{label}]({url})" if url else label


def bullet_list(lines: list[str], empty: str = "Rien à afficher.") -> str:
    return "\n".join(f"• {line}" for line in lines) if lines else f"*{empty}*"


def short_sha(sha: object) -> str:
    return str(sha or "")[:8]


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {seconds:02d} s"


def format_uptime(delta: dt.timedelta) -> str:
    minutes, _ = divmod(int(delta.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days} j {hours} h {minutes} min" if days else f"{hours} h {minutes} min"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"
