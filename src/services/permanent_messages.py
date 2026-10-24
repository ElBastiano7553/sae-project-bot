"""Publication idempotente des messages permanents.

Règle centrale : **un message permanent = une ligne en base**, identifiée par une
clé logique stable (le nom du fichier de ``content/messages/``). À chaque
synchronisation, le bot compare l'empreinte du contenu attendu avec celle
enregistrée, puis choisit une seule action :

============  ==============================================================
Action        Situation
============  ==============================================================
UNCHANGED     le message existe et son contenu est à jour : rien à faire
UPDATE        le message existe mais le contenu a changé : on le modifie
              sur place (même lien Discord, réactions conservées)
RECREATE      le message enregistré a été supprimé : on le republie
ADOPT         aucune ligne en base, mais un message du bot porte la bonne
              référence dans le salon (base perdue) : on le reprend
CREATE        aucun message connu : on le publie
MOVE          le salon associé a changé : on publie dans le nouveau salon,
              puis on supprime l'ancien message du bot (pas de doublon)
REBUILD       reconstruction demandée par un administrateur : nouveau
              message, puis suppression de l'ancien
SKIP          le salon est introuvable : rien n'est publié
ERROR         Discord ou la configuration a renvoyé une erreur
============  ==============================================================

Garanties :

* une deuxième exécution ne crée jamais de doublon ;
* toutes les opérations passent par un même verrou : deux actualisations
  simultanées (``/admin actualiser`` pendant l'actualisation du démarrage, par exemple)
  s'exécutent l'une après l'autre au lieu de publier deux fois ;
* la base n'est modifiée qu'après un succès côté Discord : en cas d'erreur, elle
  désigne toujours le dernier message publié avec succès ;
* lors d'un déplacement ou d'une reconstruction, le nouveau message est publié
  et enregistré **avant** la suppression de l'ancien : un échec ne fait jamais
  disparaître le message ;
* le bot ne supprime que ses propres messages permanents, jamais ceux des membres ;
* un message dont le fichier de contenu a été renommé (``previous_keys``) est
  repris sous sa nouvelle clé, puis déplacé si son salon a changé : jamais de doublon ;
* un salon facultatif absent peut avoir un salon de repli (``fallback`` dans
  ``config/server.toml``) : le message y est publié, puis déplacé automatiquement
  dans le bon salon dès que celui-ci est associé.

Le service ne connaît pas discord.py : il passe par l'interface
``MessageGateway``, ce qui permet de le tester avec une fausse implémentation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.config import ConfigError
from src.database.repositories import ManagedMessage, ManagedMessageRepository
from src.services.content import MessagePayload, MessageTemplate

log = logging.getLogger(__name__)


class GatewayError(Exception):
    """Erreur lors d'un échange avec Discord (permissions, réseau…). Le message est lisible par un admin."""


class MessageNotFound(GatewayError):
    """Le message visé n'existe plus (supprimé entre-temps)."""


class MessageGateway(Protocol):
    """Opérations Discord nécessaires au service. Implémentée par ``discord_bot.gateway``."""

    async def message_exists(self, channel_id: int, message_id: int) -> bool: ...

    async def find_managed_message(self, channel_id: int, keys: tuple[str, ...]) -> int | None:
        """Message du bot dont le pied de page désigne l'une de ces clés (la clé actuelle, puis les anciennes)."""
        ...

    async def send(self, channel_id: int, payload: MessagePayload) -> int: ...

    async def edit(self, channel_id: int, message_id: int, payload: MessagePayload) -> None: ...

    async def pin(self, channel_id: int, message_id: int) -> None: ...

    async def delete(self, channel_id: int, message_id: int) -> None:
        """Supprime un message du bot ; ne lève pas d'erreur s'il n'existe déjà plus."""
        ...


class SyncAction(StrEnum):
    UNCHANGED = "unchanged"
    UPDATE = "update"
    RECREATE = "recreate"
    ADOPT = "adopt"
    CREATE = "create"
    MOVE = "move"
    REBUILD = "rebuild"
    SKIP = "skip"
    ERROR = "error"


ACTION_LABELS: dict[SyncAction, str] = {
    SyncAction.UNCHANGED: "à jour",
    SyncAction.UPDATE: "mise à jour",
    SyncAction.RECREATE: "recréation (message supprimé)",
    SyncAction.ADOPT: "reprise d'un message existant",
    SyncAction.CREATE: "création",
    SyncAction.MOVE: "déplacement vers le nouveau salon",
    SyncAction.REBUILD: "reconstruction",
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


@dataclass(frozen=True)
class MessageStatus:
    """État d'un message permanent d'après la base et le contenu attendu (aucun appel à Discord)."""

    key: str
    channel_key: str
    channel_id: int | None
    record: ManagedMessage | None
    missing_links: tuple[str, ...] = ()
    error: str | None = None

    @property
    def published(self) -> bool:
        return self.record is not None


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
        lock: asyncio.Lock | None = None,
        fallbacks: Mapping[str, str] | None = None,
    ) -> None:
        self._repo = repository
        self._gateway = gateway
        self._templates = templates
        self._render = renderer
        self._pin = pin_messages
        # Le bot passe le même verrou à chaque service qu'il construit : c'est lui qui empêche
        # deux actualisations simultanées de publier chacune leur exemplaire.
        self._lock = lock or asyncio.Lock()
        # Salon de repli d'un salon facultatif absent (« regles » → « annonces »).
        self._fallbacks = dict(fallbacks or {})

    @property
    def templates(self) -> list[MessageTemplate]:
        return list(self._templates)

    def template(self, key: str) -> MessageTemplate | None:
        return next((t for t in self._templates if t.key == key), None)

    def channel_for(self, template: MessageTemplate, bindings: dict[str, int]) -> int | None:
        """Salon où publier : celui de la fonction, sinon son salon de repli."""
        channel_id = bindings.get(template.channel_key)
        if channel_id is None and template.channel_key in self._fallbacks:
            channel_id = bindings.get(self._fallbacks[template.channel_key])
        return channel_id

    def _record(self, template: MessageTemplate, *, apply: bool) -> ManagedMessage | None:
        """Message enregistré pour ce modèle, y compris sous une ancienne clé (renommée si ``apply``)."""
        record = self._repo.get(template.key)
        if record is not None:
            return record
        for old_key in template.previous_keys:
            old = self._repo.get(old_key)
            if old is None:
                continue
            if apply and self._repo.rename(old_key, template.key):
                log.info("Message permanent « %s » repris sous sa nouvelle clé « %s »", old_key, template.key)
                return self._repo.get(template.key)
            return old
        return None

    async def plan(self, bindings: dict[str, int], keys: Iterable[str] | None = None) -> list[SyncResult]:
        """Prévisualisation : calcule les actions sans rien modifier (ni Discord, ni la base)."""
        async with self._lock:
            return [await self._process(t, bindings, apply=False) for t in self._select(keys)]

    async def sync(self, bindings: dict[str, int], keys: Iterable[str] | None = None) -> list[SyncResult]:
        """Applique les actions nécessaires pour que chaque message (ou ceux de ``keys``) soit présent et à jour."""
        async with self._lock:
            return [await self._process(t, bindings, apply=True) for t in self._select(keys)]

    async def rebuild(self, key: str, bindings: dict[str, int], *, apply: bool = True) -> SyncResult:
        """Reconstruction explicite : publie un nouveau message puis supprime l'ancien.

        Réservée aux cas où le message ne peut plus être modifié, ou à la demande d'un administrateur
        (après confirmation). Le lien Discord et les réactions de l'ancien message sont perdus.
        """
        template = self.template(key)
        if template is None:
            raise ConfigError(f"Message permanent inconnu : « {key} ».")
        channel_id = self.channel_for(template, bindings)
        if channel_id is None:
            return self._result(template, SyncAction.SKIP, detail="salon introuvable : lancez /admin installer")
        async with self._lock:
            record = self._record(template, apply=apply)
            try:
                payload = self._render(template)
                if not apply:
                    return self._result(template, SyncAction.REBUILD, channel_id, record.message_id if record else None)
                return await self._publish(template, channel_id, payload, SyncAction.REBUILD, replaces=record)
            except (ConfigError, GatewayError) as exc:
                return self._error(template, channel_id, exc)

    def status(self, bindings: dict[str, int]) -> list[MessageStatus]:
        """Vue d'ensemble pour /admin messages et /etat : publication, liens manquants, dernière erreur."""
        statuses = []
        for template in self._templates:
            record = self._record(template, apply=False)
            error = record.last_error if record else None
            missing: tuple[str, ...] = ()
            try:
                missing = self._render(template).missing_links
            except ConfigError as exc:
                error = str(exc)
            statuses.append(
                MessageStatus(template.key, template.channel_key, self.channel_for(template, bindings), record, missing, error)
            )
        return statuses

    def find_by_message_id(self, message_id: int) -> ManagedMessage | None:
        """Permet de signaler la suppression d'un message géré."""
        return self._repo.get_by_message_id(message_id)

    def _select(self, keys: Iterable[str] | None) -> list[MessageTemplate]:
        if keys is None:
            return self._templates
        wanted = set(keys)
        unknown = wanted - {t.key for t in self._templates}
        if unknown:
            raise ConfigError(f"Message permanent inconnu : {', '.join(sorted(unknown))}.")
        return [t for t in self._templates if t.key in wanted]

    async def _process(self, template: MessageTemplate, bindings: dict[str, int], *, apply: bool) -> SyncResult:
        channel_id = self.channel_for(template, bindings)
        if channel_id is None:
            return self._result(template, SyncAction.SKIP, detail="salon introuvable : lancez /admin installer pour vérifier la structure")
        try:
            payload = self._render(template)
            return await self._reconcile(template, channel_id, payload, apply=apply)
        except (ConfigError, GatewayError) as exc:
            if apply:
                return self._error(template, channel_id, exc)
            return self._result(template, SyncAction.ERROR, channel_id, detail=str(exc))

    def _error(self, template: MessageTemplate, channel_id: int, exc: Exception) -> SyncResult:
        kind = "Contenu invalide" if isinstance(exc, ConfigError) else "Erreur Discord"
        log.error("%s pour le message permanent %s : %s", kind, template.key, exc)
        self._repo.record_check(template.key, SyncAction.ERROR, str(exc)[:300])
        return self._result(template, SyncAction.ERROR, channel_id, detail=str(exc))

    async def _reconcile(
        self, template: MessageTemplate, channel_id: int, payload: MessagePayload, *, apply: bool
    ) -> SyncResult:
        new_hash = payload.content_hash()
        record = self._record(template, apply=apply)

        if record is not None and record.channel_id == channel_id:
            if await self._gateway.message_exists(channel_id, record.message_id):
                if record.content_hash == new_hash:
                    if apply:
                        self._repo.record_check(template.key, SyncAction.UNCHANGED)
                    return self._result(template, SyncAction.UNCHANGED, channel_id, record.message_id)
                if apply:
                    try:
                        await self._gateway.edit(channel_id, record.message_id, payload)
                    except MessageNotFound:
                        # Supprimé entre la vérification et la modification : on le republie.
                        return await self._publish(template, channel_id, payload, SyncAction.RECREATE)
                    self._repo.upsert(template.key, channel_id, record.message_id, new_hash, SyncAction.UPDATE)
                return self._result(template, SyncAction.UPDATE, channel_id, record.message_id)
            if not apply:
                return self._result(template, SyncAction.RECREATE, channel_id)
            return await self._publish(template, channel_id, payload, SyncAction.RECREATE)

        if record is not None:
            if not apply:
                return self._result(
                    template, SyncAction.MOVE, channel_id,
                    detail=f"l'ancien message du bot dans <#{record.channel_id}> sera supprimé",
                )
            return await self._publish(template, channel_id, payload, SyncAction.MOVE, replaces=record)

        existing_id = await self._gateway.find_managed_message(channel_id, (template.key, *template.previous_keys))
        if existing_id is not None:
            if apply:
                await self._gateway.edit(channel_id, existing_id, payload)
                self._repo.upsert(template.key, channel_id, existing_id, new_hash, SyncAction.ADOPT)
            return self._result(template, SyncAction.ADOPT, channel_id, existing_id)

        if not apply:
            return self._result(template, SyncAction.CREATE, channel_id)
        return await self._publish(template, channel_id, payload, SyncAction.CREATE)

    async def _publish(
        self,
        template: MessageTemplate,
        channel_id: int,
        payload: MessagePayload,
        action: SyncAction,
        *,
        replaces: ManagedMessage | None = None,
    ) -> SyncResult:
        detail = ""
        message_id = await self._gateway.send(channel_id, payload)
        # On enregistre immédiatement : même si l'épinglage ou la suppression échoue, il n'y aura pas de doublon.
        self._repo.upsert(template.key, channel_id, message_id, payload.content_hash(), action)
        if self._pin:
            try:
                await self._gateway.pin(channel_id, message_id)
            except GatewayError as exc:
                log.warning("Impossible d'épingler %s : %s", template.key, exc)
                detail = _join(detail, "épinglage impossible (permission « Épingler des messages » ?)")
        if replaces is not None and replaces.message_id != message_id:
            try:
                await self._gateway.delete(replaces.channel_id, replaces.message_id)
            except GatewayError as exc:
                log.warning("Ancien message %s non supprimé : %s", template.key, exc)
                detail = _join(
                    detail,
                    f"l'ancien message dans <#{replaces.channel_id}> n'a pas pu être supprimé ({exc}) : "
                    "supprimez-le à la main",
                )
            else:
                detail = _join(detail, f"ancien message supprimé dans <#{replaces.channel_id}>")
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
