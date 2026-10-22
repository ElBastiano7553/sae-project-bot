"""Limites de taille imposées par Discord et fonctions pour les respecter.

Discord refuse un message dont un champ dépasse sa limite : plutôt que de
perdre une notification, on tronque proprement en signalant la coupure.
Référence : https://discord.com/developers/docs/resources/message#embed-object-embed-limits
"""

from __future__ import annotations

from collections.abc import Iterable

MESSAGE_CONTENT = 2000
EMBED_TITLE = 256
EMBED_DESCRIPTION = 4096
EMBED_FIELDS = 25
FIELD_NAME = 256
FIELD_VALUE = 1024
EMBED_FOOTER = 2048
EMBED_TOTAL = 6000
BUTTON_LABEL = 80
BUTTONS_PER_MESSAGE = 25

ELLIPSIS = "…"


def truncate(text: str, limit: int, suffix: str = ELLIPSIS) -> str:
    """Coupe ``text`` pour qu'il fasse au plus ``limit`` caractères, suffixe compris."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


def chunk_lines(lines: Iterable[str], limit: int) -> list[str]:
    """Regroupe des lignes en blocs de ``limit`` caractères au plus, sans couper une ligne en deux.

    Une ligne plus longue que ``limit`` est elle-même tronquée.
    """
    chunks: list[str] = []
    current = ""
    for raw_line in lines:
        line = truncate(raw_line, limit)
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks
