"""File d'attente entre la réception d'un webhook GitHub et l'envoi sur Discord.

Même fonctionnement que pour GitLab (``src/gitlab/dispatcher.py``) : le serveur
répond tout de suite à GitHub (qui abandonne au bout de 10 secondes), l'envoi sur
Discord se fait ensuite dans une tâche de fond (``run``).

Garanties :

* une livraison n'est enregistrée (donc « déjà reçue ») que si elle a bien été mise
  en file ; sinon elle est oubliée pour que GitHub puisse la renvoyer ;
* la notification est enregistrée avec la livraison : si le bot s'arrête avant
  l'envoi, elle est renvoyée au redémarrage (``restore_pending``) ;
* un envoi Discord en échec est retenté (``RETRY_DELAYS``), puis marqué ``failed``
  (tout de suite si le refus est définitif) ;
* aucune erreur ne peut arrêter la tâche de fond.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from src.database.github import GitHubDeliveryRepository
from src.github.events import format_event
from src.gitlab.dispatcher import (
    DEFAULT_QUEUE_SIZE,
    REPLAY_MAX_AGE_HOURS,
    RETRY_DELAYS,
    EventStatus,
    NotificationSender,
    SubmitStatus,
    notification_from_json,
    notification_to_json,
    send_with_retries,
)
from src.gitlab.events import Notification

log = logging.getLogger(__name__)

# Salon des notifications GitHub (clé de config/server.toml) : le même que l'activité GitLab.
CHANNEL_KEY = "gitlab_activite"


@dataclass(frozen=True)
class GitHubDelivery:
    event: str  # en-tête X-GitHub-Event
    delivery_id: str  # en-tête X-GitHub-Delivery
    payload: dict

    @property
    def key(self) -> str:
        return f"{self.event}:{self.delivery_id}"


@dataclass(frozen=True)
class _Job:
    key: str
    notification: Notification


class GitHubDispatcher:
    def __init__(
        self,
        repository: GitHubDeliveryRepository,
        sender: NotificationSender,
        bindings: Callable[[], dict[str, int]],
        *,
        channel_key: str = CHANNEL_KEY,
        wait_until_ready: Callable[[], Awaitable[object]] | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ) -> None:
        self.repository = repository
        self.sender = sender
        self.channel_key = channel_key
        self._bindings = bindings
        self._wait_until_ready = wait_until_ready
        self._retry_delays = tuple(retry_delays)
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_size)

    def submit(self, delivery: GitHubDelivery) -> SubmitStatus:
        """Met en forme, déduplique et met en file. Lève ``InvalidPayload`` si le JSON est incomplet."""
        notification = format_event(delivery.event, delivery.payload)
        if notification is None:
            return SubmitStatus.IGNORED
        claimed = self.repository.claim(
            delivery.key,
            delivery.event,
            notification_to_json(notification),
            dedup_key=notification.dedup_key,
            title=notification.title,
            url=notification.url,
        )
        if not claimed:
            log.info("Livraison GitHub déjà reçue, ignorée : %s", delivery.key)
            return SubmitStatus.DUPLICATE
        try:
            self._queue.put_nowait(_Job(delivery.key, notification))
        except asyncio.QueueFull:
            # On oublie la livraison pour que GitHub puisse la renvoyer plus tard.
            self.repository.release(delivery.key)
            log.warning("File des notifications GitHub pleine : livraison %s refusée", delivery.key)
            return SubmitStatus.BUSY
        return SubmitStatus.QUEUED

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def restore_pending(self) -> int:
        """Remet en file les notifications acceptées mais jamais envoyées (bot arrêté avant l'envoi)."""
        expired = self.repository.expire_queued(REPLAY_MAX_AGE_HOURS)
        if expired:
            log.warning("%d notification(s) GitHub trop ancienne(s) abandonnée(s)", expired)
        restored = 0
        for key, raw in self.repository.pending(REPLAY_MAX_AGE_HOURS):
            try:
                self._queue.put_nowait(_Job(key, notification_from_json(raw)))
            except (TypeError, ValueError, KeyError):
                log.error("Notification GitHub %s illisible : abandonnée", key)
                self.repository.set_status(key, EventStatus.FAILED)
            except asyncio.QueueFull:
                log.error("File pleine : notification GitHub %s abandonnée", key)
                self.repository.set_status(key, EventStatus.FAILED)
            else:
                restored += 1
        if restored:
            log.info("%d notification(s) GitHub non envoyée(s) avant l'arrêt remise(s) en file", restored)
        return restored

    async def run(self) -> None:
        """Tâche de fond : envoie les notifications une par une, sans jamais s'arrêter sur une erreur."""
        if self._wait_until_ready is not None:
            await self._wait_until_ready()
        while True:
            job = await self._queue.get()
            try:
                await self.deliver(job)
            except Exception:  # dernier filet : la tâche ne doit jamais mourir
                log.exception("Erreur inattendue en traitant la notification GitHub %s", job.key)
            finally:
                self._queue.task_done()

    async def deliver(self, job: _Job) -> EventStatus:
        channel_id = self._bindings().get(self.channel_key)
        if channel_id is None:
            log.warning(
                "Notification GitHub non publiée : le salon « %s » n'est pas associé (lancez /admin installer).", self.channel_key
            )
            status = EventStatus.SKIPPED
        else:
            status = await send_with_retries(self.sender, channel_id, job.notification, self._retry_delays, f"GitHub {job.key}")
        self.repository.set_status(job.key, status)
        return status

    async def drain(self) -> None:
        """Traite immédiatement les notifications en attente (utilisé par les tests)."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            try:
                await self.deliver(job)
            finally:
                self._queue.task_done()
