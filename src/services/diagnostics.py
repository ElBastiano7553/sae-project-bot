"""Vérification des permissions du bot dans les salons qu'il utilise (pour /admin diagnostic).

La logique est indépendante de Discord : elle reçoit, pour chaque salon, les
permissions accordées au bot sous forme de dictionnaire.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

BASE_PERMISSIONS = {
    "view_channel": "Voir le salon",
    "send_messages": "Envoyer des messages",
    "embed_links": "Intégrer des liens",
}
# Nécessaire pour vérifier qu'un message permanent existe encore et pour le retrouver si la base est perdue.
HISTORY_PERMISSION = {"read_message_history": "Voir les anciens messages"}
PIN_PERMISSION = {"pin_messages": "Épingler des messages"}


@dataclass(frozen=True)
class ChannelDiagnostic:
    channel_key: str
    channel_id: int | None
    uses: tuple[str, ...]
    missing: tuple[str, ...] = ()
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.problem


def required_permissions(uses: Collection[str], pin_messages: bool) -> dict[str, str]:
    required = dict(BASE_PERMISSIONS)
    if "messages" in uses:
        required.update(HISTORY_PERMISSION)
        if pin_messages:
            required.update(PIN_PERMISSION)
    return required


def missing_permissions(granted: Mapping[str, bool], required: Mapping[str, str]) -> list[str]:
    missing = []
    for name, label in required.items():
        allowed = granted.get(name, False)
        if name == "pin_messages":
            # Avant 2025, épingler dépendait de « Gérer les messages » : les deux sont acceptés.
            allowed = allowed or granted.get("manage_messages", False)
        if not allowed:
            missing.append(label)
    return missing


def channel_uses(message_channels: Collection[str], notification_channels: Collection[str]) -> dict[str, tuple[str, ...]]:
    """Pour chaque salon utilisé : « messages » (messages permanents) et/ou « notifications » (GitLab)."""
    uses: dict[str, list[str]] = {}
    for use, keys in (("messages", message_channels), ("notifications", notification_channels)):
        for key in keys:
            if use not in uses.setdefault(key, []):
                uses[key].append(use)
    return {key: tuple(value) for key, value in uses.items()}
