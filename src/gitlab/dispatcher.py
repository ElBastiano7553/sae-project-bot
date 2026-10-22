"""File d'attente entre la réception d'un webhook et l'envoi sur Discord.

GitLab attend une réponse en moins de 10 secondes et désactive un webhook qui
échoue trop souvent : le serveur répond donc tout de suite, et l'envoi sur
Discord se fait ensuite, dans une tâche de fond (``run``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.database.repositories import GitLabEventRepository
from src.gitlab.events import Notification, format_event
from src.gitlab.rules import GitLabRules

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 100
# Durée de conservation des clés d'événements (GitLab ne renvoie pas un événement aussi vieux).
RETENTION_DAYS = 30


class NotificationSender(Protocol):
    async def send_notification(self, channel_id: int, notification: Notification) -> None:
        """Publie la notification ; lève une exception en cas d'échec."""


class SubmitStatus(StrEnum):
    QUEUED = "queued"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    BUSY = "busy"


class EventStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class IncomingEvent:
    kind: str
    delivery_key: str
    payload: dict

    @property
    def event_key(self) -> str:
        return f"{self.kind}:{self.delivery_key}"


@dataclass(frozen=True)
class _Job:
    event_key: str
    notification: Notification


class NotificationDispatcher:
    def __init__(
        self,
        repository: GitLabEventRepository,
        rules: GitLabRules,
        sender: NotificationSender,
        bindings: Callable[[], dict[str, int]],
        *,
        wait_until_ready: Callable[[], Awaitable[object]] | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.repository = repository
        self.rules = rules
        self.sender = sender
        self._bindings = bindings
        self._wait_until_ready = wait_until_ready
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_size)

    def submit(self, event: IncomingEvent) -> SubmitStatus:
        """Valide, déduplique et met en file. Lève ``InvalidPayload`` si le JSON est incomplet."""
        notification = format_event(event.kind, event.payload, self.rules)
        if notification is None:
            return SubmitStatus.IGNORED
        if not self.repository.claim(event.event_key, event.kind):
            log.info("Événement GitLab déjà reçu, ignoré : %s", event.event_key)
            return SubmitStatus.DUPLICATE
        try:
            self._queue.put_nowait(_Job(event.event_key, notification))
        except asyncio.QueueFull:
            # On oublie l'événement pour que GitLab puisse le renvoyer plus tard.
            self.repository.release(event.event_key)
            log.warning("File des notifications GitLab pleine : événement %s refusé", event.event_key)
            return SubmitStatus.BUSY
        return SubmitStatus.QUEUED

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def run(self) -> None:
        """Tâche de fond : envoie les notifications une par une, sans jamais s'arrêter sur une erreur."""
        if self._wait_until_ready is not None:
            await self._wait_until_ready()
        while True:
            job = await self._queue.get()
            try:
                await self.deliver(job)
            finally:
                self._queue.task_done()

    async def deliver(self, job: _Job) -> EventStatus:
        notification = job.notification
        route = self.rules.routes[notification.kind]
        channel_id = self._bindings().get(route)
        if channel_id is None:
            log.warning(
                "Notification GitLab « %s » non publiée : le salon « %s » n'est pas associé (lancez /setup).",
                notification.kind,
                route,
            )
            status = EventStatus.SKIPPED
        else:
            try:
                await self.sender.send_notification(channel_id, notification)
            except Exception:
                log.exception("Échec de l'envoi de la notification GitLab %s", job.event_key)
                status = EventStatus.FAILED
            else:
                status = EventStatus.SENT
        self.repository.set_status(job.event_key, status)
        return status

    async def drain(self) -> None:
        """Traite immédiatement les notifications en attente (utilisé par les tests)."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            await self.deliver(job)
            self._queue.task_done()
