"""Message d'accueil des nouveaux membres : contenu et suppression automatique (sans discord.py).

Le message est publié par Samae quand un membre rejoint le serveur (``WELCOME_ON_JOIN``),
puis supprimé après ``WELCOME_DELETE_AFTER_HOURS`` heures. Seuls les messages d'accueil
enregistrés ici sont supprimés : jamais un autre message du salon.

La liste d'attente est gardée dans la table clé-valeur existante (clé ``welcome_pending``) :
aucune migration de la base, et un redémarrage du bot ne fait rien oublier.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from src.config import CONTENT_DIR, ConfigError
from src.services.content import MessageTemplate, _parse_template

log = logging.getLogger(__name__)

WELCOME_FILE = CONTENT_DIR.parent / "accueil.toml"
PENDING_KEY = "welcome_pending"
# Au-delà, les plus anciens sont oubliés (et restent dans le salon) : protège contre un afflux massif.
MAX_PENDING = 500
MEMBER_PLACEHOLDER = "{{membre}}"


@dataclass(frozen=True)
class PendingWelcome:
    channel_id: int
    message_id: int
    member_id: int
    delete_at: str  # ISO 8601 UTC

    def due(self, now: dt.datetime) -> bool:
        try:
            moment = dt.datetime.fromisoformat(self.delete_at)
        except ValueError:
            return True  # date illisible : on ne garde pas une entrée qu'on ne saurait jamais traiter
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        return moment <= now


def load_welcome_template(path: Path = WELCOME_FILE) -> MessageTemplate:
    """Texte du message d'accueil (même format qu'un message permanent, ``{{membre}}`` en plus)."""
    if not path.exists():
        raise ConfigError(f"{path.name} est introuvable : le message d'accueil ne peut pas être publié.")
    return _parse_template(path)


def personalize(template: MessageTemplate, member_mention: str) -> MessageTemplate:
    """Remplace ``{{membre}}`` (titre, texte et champs) par la mention du nouveau membre."""

    def fill(text: str) -> str:
        return text.replace(MEMBER_PLACEHOLDER, member_mention)

    return replace(
        template,
        title=fill(template.title),
        description=fill(template.description),
        fields=tuple(replace(f, name=fill(f.name), value=fill(f.value)) for f in template.fields),
    )


def load_pending(raw: str | None) -> list[PendingWelcome]:
    if not raw:
        return []
    try:
        return [PendingWelcome(**item) for item in json.loads(raw)]
    except (TypeError, ValueError):
        log.error("Liste des messages d'accueil à supprimer illisible : elle est réinitialisée")
        return []


def dump_pending(items: list[PendingWelcome]) -> str:
    return json.dumps([asdict(item) for item in items[-MAX_PENDING:]])


def split_due(items: list[PendingWelcome], now: dt.datetime) -> tuple[list[PendingWelcome], list[PendingWelcome]]:
    """(messages à supprimer maintenant, messages à garder pour plus tard)."""
    due = [item for item in items if item.due(now)]
    return due, [item for item in items if not item.due(now)]


def delete_time(now: dt.datetime, hours: int) -> str:
    return (now + dt.timedelta(hours=hours)).isoformat(timespec="seconds")
