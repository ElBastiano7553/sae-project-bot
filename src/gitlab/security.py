"""Fonctions de sécurité pour les webhooks GitLab.

Tout ce qui vient de GitLab (messages de commit, titres de Merge Request, noms
de branches…) est écrit par des humains et doit être traité comme une donnée
non fiable : on le nettoie avant de l'afficher sur Discord.
"""

from __future__ import annotations

import hmac
import re

from src.services import text_limits as limits

# Caractères interprétés par le Markdown de Discord, partout ou seulement en début de ligne.
_MARKDOWN_CHARS = re.compile(r"([\\*_~`|\[\]])")
_LINE_START_MARKDOWN = re.compile(r"^(\s*)([>#-])", re.MULTILINE)
# Caractères de contrôle (sauf saut de ligne) : invisibles ou perturbants à l'affichage.
_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
# Mentions Discord (@everyone, @here, <@123>, <@&123>, <#123>).
_MENTIONS = re.compile(r"@(everyone|here)|<(@[!&]?|#)(\d+)>")
_ZERO_WIDTH_SPACE = "\u200b"


def token_matches(expected: str, received: str | None) -> bool:
    """Compare le secret reçu au secret attendu en temps constant (pas de fuite par le chronométrage)."""
    if not expected or received is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), received.encode("utf-8"))


def clean_text(value: object, limit: int, *, single_line: bool = False) -> str:
    """Rend un texte GitLab inoffensif : pas de Markdown, pas de mention, longueur bornée."""
    text = value if isinstance(value, str) else ""
    text = _CONTROL_CHARS.sub("", text).replace("\r", "")
    if single_line:
        text = " ".join(text.split())
    text = _MENTIONS.sub(lambda m: m.group(0).replace("@", "@" + _ZERO_WIDTH_SPACE).replace("<", "<" + _ZERO_WIDTH_SPACE), text)
    text = _MARKDOWN_CHARS.sub(r"\\\1", text)
    text = _LINE_START_MARKDOWN.sub(r"\1\\\2", text).strip()
    return limits.truncate(text, limit)


def plain_text(value: object, limit: int) -> str:
    """Version sur une ligne, sans échappement Markdown : pour les titres d'embed, qui l'interprètent peu."""
    text = value if isinstance(value, str) else ""
    text = " ".join(_CONTROL_CHARS.sub("", text).split())
    text = _MENTIONS.sub(lambda m: m.group(0).replace("@", "@" + _ZERO_WIDTH_SPACE).replace("<", "<" + _ZERO_WIDTH_SPACE), text)
    return limits.truncate(text, limit)


def first_line(value: object) -> str:
    """Première ligne non vide d'un message (le titre d'un commit)."""
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        if line.strip():
            return line.strip()
    return ""


def safe_url(value: object) -> str | None:
    """Accepte uniquement une URL http(s) sans espace, sinon ``None`` (le lien est alors omis)."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url.startswith(("https://", "http://")) or len(url) > 2048:
        return None
    if any(ch.isspace() or ch in "<>\"'()" for ch in url):
        return None
    return url
