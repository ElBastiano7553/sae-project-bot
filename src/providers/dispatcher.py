"""Aiguillage d'un événement vers les projets concernés, puis envoi sur Discord.

Pour chaque projet visé (``database.projects.ProjectRepository.github_targets`` /
``gitlab_target``) :

1. la catégorie de l'événement doit avoir un salon pour CE projet (sinon rien) ;
2. les filtres de cette catégorie doivent le laisser passer (sinon il est noté
   ``filtered``, sans notification) ;
3. l'événement est enregistré pour ce projet (anti-doublons : même livraison ou même
   fait déjà reçu → rien), puis mis en file.

Avant l'envoi, le salon est revérifié : il doit encore exister et appartenir au serveur
Discord du projet. Un salon d'un autre serveur n'est jamais utilisé, même si la base a été
modifiée à la main.

Mêmes garanties que les webhooks historiques (``src/gitlab/dispatcher.py``) : réponse
immédiate au fournisseur, envoi en tâche de fond, renvoi après redémarrage, essais répétés,
et aucune erreur ne peut arrêter la tâche de fond.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from src.database.project_deliveries import ProjectDeliveryRepository
from src.database.projects import ProjectRecord, ProjectRepository
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
from src.providers.translate import ProjectEvent
from src.services.projects import route_allows

log = logging.getLogger(__name__)

FILTERED = "filtered"


@dataclass(frozen=True)
class _Job:
    project_id: int
    guild_id: int
    delivery_key: str
    channel_id: int
    notification: Notification


class ProjectDispatcher:
    def __init__(
        self,
        projects: ProjectRepository,
        deliveries: ProjectDeliveryRepository,
        sender: NotificationSender,
        channel_guild: Callable[[int], Awaitable[int | None]],
        *,
        wait_until_ready: Callable[[], Awaitable[object]] | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ) -> None:
        """``channel_guild(channel_id)`` : serveur Discord du salon, ou None s'il est introuvable."""
        self.projects = projects
        self.deliveries = deliveries
        self.sender = sender
        self._channel_guild = channel_guild
        self._wait_until_ready = wait_until_ready
        self._retry_delays = tuple(retry_delays)
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_size)

    def submit(self, event: ProjectEvent, targets: Iterable[ProjectRecord]) -> SubmitStatus:
        """Enregistre et met en file l'événement pour chaque projet visé. Résume le résultat en un statut."""
        results: list[SubmitStatus] = []
        for project in targets:
            results.append(self._submit_one(event, project))
        if not results:
            return SubmitStatus.IGNORED
        for status in (SubmitStatus.BUSY, SubmitStatus.QUEUED, SubmitStatus.RECORDED, SubmitStatus.DUPLICATE):
            if status in results:
                return status
        return SubmitStatus.IGNORED

    def _submit_one(self, event: ProjectEvent, project: ProjectRecord) -> SubmitStatus:
        # Salons et filtres lus avec le serveur DU projet : jamais ceux d'un autre serveur.
        route = next((r for r in self.projects.routes(project.guild_id, project.id) if r.category == event.category), None)
        if route is None:
            return SubmitStatus.IGNORED  # catégorie non publiée pour ce projet
        if not route_allows(event.category, route.filters, value=event.value, branch=event.branch):
            claimed = self.deliveries.claim(
                project.id, project.guild_id, event.delivery_key, event.category,
                dedup_key=event.dedup_key, channel_id=None, notification=None, status=FILTERED,
            )
            return SubmitStatus.RECORDED if claimed else SubmitStatus.DUPLICATE
        claimed = self.deliveries.claim(
            project.id, project.guild_id, event.delivery_key, event.category,
            dedup_key=event.dedup_key, channel_id=route.channel_id, notification=notification_to_json(event.notification),
        )
        if not claimed:
            log.info("Événement déjà reçu pour le projet %d, ignoré : %s", project.id, event.delivery_key)
            return SubmitStatus.DUPLICATE
        try:
            self._queue.put_nowait(_Job(project.id, project.guild_id, event.delivery_key, route.channel_id, event.notification))
        except asyncio.QueueFull:
            # Oubliée pour que le fournisseur puisse la renvoyer plus tard.
            self.deliveries.release(project.id, event.delivery_key)
            log.warning("File des notifications des projets pleine : %s refusé pour le projet %d", event.delivery_key, project.id)
            return SubmitStatus.BUSY
        return SubmitStatus.QUEUED

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def restore_pending(self) -> int:
        """Remet en file les notifications acceptées mais jamais envoyées (bot arrêté avant l'envoi)."""
        expired = self.deliveries.expire_queued(REPLAY_MAX_AGE_HOURS)
        if expired:
            log.warning("%d notification(s) de projets trop ancienne(s) abandonnée(s)", expired)
        restored = 0
        for project_id, guild_id, key, channel_id, raw in self.deliveries.pending(REPLAY_MAX_AGE_HOURS):
            try:
                if channel_id is None:
                    raise ValueError("salon absent")
                self._queue.put_nowait(_Job(project_id, guild_id, key, channel_id, notification_from_json(raw)))
            except (TypeError, ValueError, KeyError):
                log.error("Notification %s du projet %d illisible : abandonnée", key, project_id)
                self.deliveries.set_status(project_id, key, EventStatus.FAILED)
            except asyncio.QueueFull:
                log.error("File pleine : notification %s du projet %d abandonnée", key, project_id)
                self.deliveries.set_status(project_id, key, EventStatus.FAILED)
            else:
                restored += 1
        if restored:
            log.info("%d notification(s) de projets non envoyée(s) avant l'arrêt remise(s) en file", restored)
        return restored

    async def run(self) -> None:
        if self._wait_until_ready is not None:
            await self._wait_until_ready()
        while True:
            job = await self._queue.get()
            try:
                await self.deliver(job)
            except Exception:  # dernier filet : la tâche ne doit jamais mourir
                log.exception("Erreur inattendue en traitant la notification %s du projet %d", job.delivery_key, job.project_id)
            finally:
                self._queue.task_done()

    async def deliver(self, job: _Job) -> EventStatus:
        guild_id = await self._channel_guild(job.channel_id)
        if guild_id != job.guild_id:
            log.warning(
                "Notification %s du projet %d non publiée : le salon %d est introuvable ou n'appartient pas au serveur du projet.",
                job.delivery_key, job.project_id, job.channel_id,
            )
            status = EventStatus.SKIPPED
        else:
            status = await send_with_retries(
                self.sender, job.channel_id, job.notification, self._retry_delays, f"{job.delivery_key} du projet {job.project_id}"
            )
        self.deliveries.set_status(job.project_id, job.delivery_key, status)
        return status

    async def drain(self) -> None:
        """Traite immédiatement les notifications en attente (utilisé par les tests)."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            try:
                await self.deliver(job)
            finally:
                self._queue.task_done()
