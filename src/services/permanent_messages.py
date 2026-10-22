"""Publication idempotente des messages permanents.

Règle centrale : **un message permanent = une ligne en base**. À chaque
synchronisation, le bot compare l'empreinte du contenu attendu avec celle
enregistrée, puis choisit une seule action :

============  ==============================================================
Action        Situation
============  ==============================================================
UNCHANGED     le message existe et son contenu est à jour : rien à faire
UPDATE        le message existe mais le contenu a changé : on le modifie
RECREATE      le message enregistré a été supprimé : on le republie
ADOPT         aucune ligne en base, mais un message du bot porte la bonne
              référence dans le salon (base perdue) : on le reprend
CREATE        aucun message connu : on le publie
MOVE          le salon associé a changé : on publie dans le nouveau salon
SKIP          le salon est introuvable : rien n'est publié
ERROR         Discord ou la configuration a renvoyé une erreur
============  ==============================================================

Une deuxième exécution ne crée donc jamais de doublon. Le bot ne supprime
jamais de message, même l'ancien message en cas de MOVE.

Le service ne connaît pas discord.py : il passe par l'interface
``MessageGateway``, ce qui permet de le tester avec une fausse implémentation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.config import ConfigError
from src.database.repositories import ManagedMessage, ManagedMessageRepository
from src.services.content import MessagePayload, MessageTemplate

log = logging.getLogger(__name__)


class GatewayError(Exception):
    """Erreur lors d'un échange avec Discord (permissions, réseau…)."""


class MessageGateway(Protocol):
    """Opérations Discord nécessaires au service. Implémentée par ``discord_bot.gateway``."""

    async def message_exists(self, channel_id: int, message_id: int) -> bool: ...

    async def find_managed_message(self, channel_id: int, key: str) -> int | None: ...

    async def send(self, channel_id: int, payload: MessagePayload) -> int: ...

    async def edit(self, channel_id: int, message_id: int, payload: MessagePayload) -> None: ...

    async def pin(self, channel_id: int, message_id: int) -> None: ...


class SyncAction(StrEnum):
    UNCHANGED = "unchanged"
    UPDATE = "update"
    RECREATE = "recreate"
    ADOPT = "adopt"
    CREATE = "create"
    MOVE = "move"
    SKIP = "skip"
    ERROR = "error"


ACTION_LABELS: dict[SyncAction, str] = {
    SyncAction.UNCHANGED: "à jour",
    SyncAction.UPDATE: "mise à jour",
    SyncAction.RECREATE: "recréation (message supprimé)",
    SyncAction.ADOPT: "reprise d'un message existant",
    SyncAction.CREATE: "création",
    SyncAction.MOVE: "publication dans le nouveau salon",
    SyncAction.SKIP: "ignoré",
    SyncAction.ERROR: "erreur",
}


@dataclass(frozen=True)
class SyncResult:
    key: str
    channel_key: str
    action: SyncAction
    channel_id: int | None = None
    message_id: int | None = None
    detail: str = ""


Renderer = Callable[[MessageTemplate], MessagePayload]


class PermanentMessageService:
    def __init__(
        self,
        repository: ManagedMessageRepository,
        gateway: MessageGateway,
        templates: list[MessageTemplate],
        renderer: Renderer,
        *,
        pin_messages: bool = True,
    ) -> None:
        self._repo = repository
        self._gateway = gateway
        self._templates = templates
        self._render = renderer
        self._pin = pin_messages

    @property
    def templates(self) -> list[MessageTemplate]:
        return list(self._templates)

    async def plan(self, bindings: dict[str, int]) -> list[SyncResult]:
        """Prévisualisation : calcule les actions sans rien modifier (ni Discord, ni la base)."""
        return [await self._process(t, bindings, apply=False) for t in self._templates]

    async def sync(self, bindings: dict[str, int]) -> list[SyncResult]:
        """Applique les actions nécessaires pour que chaque message soit présent et à jour."""
        return [await self._process(t, bindings, apply=True) for t in self._templates]

    def find_by_message_id(self, message_id: int) -> ManagedMessage | None:
        """Permet de signaler la suppression d'un message géré."""
        return self._repo.get_by_message_id(message_id)

    async def _process(self, template: MessageTemplate, bindings: dict[str, int], *, apply: bool) -> SyncResult:
        channel_id = bindings.get(template.channel_key)
        if channel_id is None:
            return self._result(template, SyncAction.SKIP, detail="salon introuvable : lancez /setup pour vérifier la structure")
        try:
            payload = self._render(template)
            return await self._reconcile(template, channel_id, payload, apply=apply)
        except ConfigError as exc:
            log.error("Contenu invalide pour %s : %s", template.key, exc)
            return self._result(template, SyncAction.ERROR, channel_id, detail=str(exc))
        except GatewayError as exc:
            log.error("Erreur Discord pour %s : %s", template.key, exc)
            return self._result(template, SyncAction.ERROR, channel_id, detail=str(exc))

    async def _reconcile(
        self, template: MessageTemplate, channel_id: int, payload: MessagePayload, *, apply: bool
    ) -> SyncResult:
        new_hash = payload.content_hash()
        record = self._repo.get(template.key)

        if record is not None and record.channel_id == channel_id:
            if await self._gateway.message_exists(channel_id, record.message_id):
                if record.content_hash == new_hash:
                    return self._result(template, SyncAction.UNCHANGED, channel_id, record.message_id)
                if apply:
                    await self._gateway.edit(channel_id, record.message_id, payload)
                    self._repo.upsert(template.key, channel_id, record.message_id, new_hash)
                return self._result(template, SyncAction.UPDATE, channel_id, record.message_id)
            return await self._publish(template, channel_id, payload, SyncAction.RECREATE, apply=apply)

        if record is not None:
            return await self._publish(
                template, channel_id, payload, SyncAction.MOVE, apply=apply,
                detail="l'ancien message n'est pas supprimé automatiquement",
            )

        existing_id = await self._gateway.find_managed_message(channel_id, template.key)
        if existing_id is not None:
            if apply:
                await self._gateway.edit(channel_id, existing_id, payload)
                self._repo.upsert(template.key, channel_id, existing_id, new_hash)
            return self._result(template, SyncAction.ADOPT, channel_id, existing_id)

        return await self._publish(template, channel_id, payload, SyncAction.CREATE, apply=apply)

    async def _publish(
        self,
        template: MessageTemplate,
        channel_id: int,
        payload: MessagePayload,
        action: SyncAction,
        *,
        apply: bool,
        detail: str = "",
    ) -> SyncResult:
        if not apply:
            return self._result(template, action, channel_id, detail=detail)
        message_id = await self._gateway.send(channel_id, payload)
        # On enregistre immédiatement : même si l'épinglage échoue, il n'y aura pas de doublon.
        self._repo.upsert(template.key, channel_id, message_id, payload.content_hash())
        if self._pin:
            try:
                await self._gateway.pin(channel_id, message_id)
            except GatewayError as exc:
                log.warning("Impossible d'épingler %s : %s", template.key, exc)
                detail = _join(detail, "épinglage impossible (permission « Épingler des messages » ?)")
        log.info("Message permanent %s : %s (salon %s, message %s)", template.key, action, channel_id, message_id)
        return self._result(template, action, channel_id, message_id, detail)

    @staticmethod
    def _result(
        template: MessageTemplate,
        action: SyncAction,
        channel_id: int | None = None,
        message_id: int | None = None,
        detail: str = "",
    ) -> SyncResult:
        return SyncResult(template.key, template.channel_key, action, channel_id, message_id, detail)


def _join(first: str, second: str) -> str:
    return f"{first} ; {second}" if first else second
