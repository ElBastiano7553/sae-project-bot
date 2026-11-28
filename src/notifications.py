"""Socle commun des trois files de notifications (GitLab, GitHub, projets).

Chaque file (``src/gitlab/dispatcher.py``, ``src/github/dispatcher.py``,
``src/providers/dispatcher.py``) reçoit un événement d'un webhook, l'enregistre en base,
puis l'envoie sur Discord dans une tâche de fond. Ce module regroupe ce qui leur est commun :

* les constantes (taille de la file, âge maximal d'un renvoi, délais entre deux essais) ;
* les statuts (``SubmitStatus`` pour la réponse au fournisseur, ``EventStatus`` en base) ;
* l'envoi avec nouveaux essais (``send_with_retries``) ;
* la (dé)sérialisation d'une ``Notification`` enregistrée en base ;
* ``NotificationQueue`` : la file, la tâche de fond qui ne meurt jamais, et l'arrêt propre
  qui laisse finir l'envoi en cours (sinon un message parti sur Discord mais pas encore
  noté ``sent`` serait renvoyé au redémarrage).

Avant la 1.15.0, ce code était dans ``src/gitlab/dispatcher.py`` et recopié dans les deux
autres files. ``src/gitlab/dispatcher.py`` le réexporte toujours.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from src.gitlab.events import Notification
from src.services.content import EmbedField

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 100
# Au-delà, une notification non envoyée (bot arrêté) n'est plus renvoyée : elle serait périmée.
REPLAY_MAX_AGE_HOURS = 24
# Attente (secondes) avant chaque nouvelle tentative d'envoi sur Discord.
RETRY_DELAYS: tuple[float, ...] = (2.0, 10.0)
# Attente maximale (secondes) de la fin de l'envoi en cours à l'arrêt du bot. systemd laisse
# 30 secondes (deploy/samae.service.in, TimeoutStopSec) pour tout l'arrêt.
STOP_TIMEOUT = 10.0


class SubmitStatus(StrEnum):
    QUEUED = "queued"
    RECORDED = "recorded"  # conservé dans l'historique, sans notification (branche non suivie…)
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    BUSY = "busy"


class EventStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
    RECORDED = "recorded"


class NotificationSender(Protocol):
    async def send_notification(self, channel_id: int, notification: Notification) -> None:
        """Publie la notification ; lève une exception en cas d'échec (``PermanentSendError`` s'il est inutile de réessayer)."""


class PermanentSendError(Exception):
    """Envoi refusé pour une raison qu'un nouvel essai ne changera pas (salon supprimé, permission manquante…)."""


async def send_with_retries(
    sender: NotificationSender, channel_id: int, notification: Notification, retry_delays: Sequence[float], label: str
) -> EventStatus:
    """Envoie la notification, avec un nouvel essai après chaque attente de ``retry_delays``.

    Un refus définitif n'est pas retenté : la file étant traitée une notification à la fois,
    chaque essai inutile retarderait toutes les autres (12 secondes par notification avec les
    délais par défaut).
    """
    for attempt, delay in enumerate((*retry_delays, None), start=1):
        try:
            await sender.send_notification(channel_id, notification)
        except PermanentSendError as exc:
            log.error("Notification %s abandonnée sans nouvel essai : %s", label, exc)
            return EventStatus.FAILED
        except Exception as exc:
            if delay is None:
                log.error("Notification %s abandonnée après %d essai(s) : %s", label, attempt, exc)
                return EventStatus.FAILED
            log.warning("Envoi de la notification %s impossible (%s), nouvel essai dans %g s", label, exc, delay)
            await asyncio.sleep(delay)
        else:
            return EventStatus.SENT
    return EventStatus.FAILED  # inatteignable, pour les vérificateurs de types


def notification_to_json(notification: Notification) -> str:
    return json.dumps(asdict(notification), ensure_ascii=False)


def notification_from_json(raw: str | None) -> Notification:
    if raw is None:
        raise ValueError("notification absente")
    data = json.loads(raw)
    data["fields"] = tuple(EmbedField(**field) for field in data.get("fields", ()))
    return Notification(**data)


Job = TypeVar("Job")


class NotificationQueue(Generic[Job]):
    """File d'envoi commune : une notification à la fois, sans jamais s'arrêter sur une erreur.

    Les sous-classes fournissent ``deliver`` (envoi d'un travail et mise à jour de son statut)
    et ``describe`` (libellé d'un travail dans les journaux).
    """

    #: Nom de la file dans les journaux (« GitLab », « GitHub », « des projets »).
    label = "?"

    def __init__(
        self,
        *,
        wait_until_ready: Callable[[], Awaitable[object]] | None,
        queue_size: int,
        retry_delays: Sequence[float],
    ) -> None:
        self._wait_until_ready = wait_until_ready
        self._retry_delays = tuple(retry_delays)
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=queue_size)
        self._stopping = False
        self._busy = False

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def busy(self) -> bool:
        """Vrai pendant le traitement d'une notification."""
        return self._busy

    def describe(self, job: Job) -> str:
        return repr(job)

    async def deliver(self, job: Job) -> EventStatus:
        raise NotImplementedError

    async def _process(self, job: Job) -> None:
        self._busy = True
        try:
            await self.deliver(job)
        except Exception:  # dernier filet : la tâche ne doit jamais mourir
            log.exception("Erreur inattendue en traitant la notification %s", self.describe(job))
        finally:
            self._busy = False
            self._queue.task_done()

    async def run(self) -> None:
        """Tâche de fond : envoie les notifications une par une jusqu'à ``stop``."""
        if self._wait_until_ready is not None:
            await self._wait_until_ready()
        while not self._stopping:
            job = await self._queue.get()
            await self._process(job)

    async def drain(self) -> None:
        """Traite immédiatement les notifications en attente (utilisé par les tests)."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            self._busy = True
            try:
                await self.deliver(job)
            finally:
                self._busy = False
                self._queue.task_done()

    async def stop(self, task: asyncio.Task | None, timeout: float = STOP_TIMEOUT) -> None:
        """Arrête la tâche de fond ``task`` sans couper un envoi en cours.

        Les notifications encore en file restent ``queued`` en base : elles seront renvoyées au
        prochain démarrage (``restore_pending``). Seul un envoi qui dure plus de ``timeout``
        secondes est interrompu (il pourra alors être renvoyé en double).
        """
        self._stopping = True
        if task is None or task.done():
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._busy and loop.time() < deadline:
            await asyncio.sleep(0.05)
        if self._busy:
            log.warning("Arrêt de la file %s : envoi en cours interrompu après %g s", self.label, timeout)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
