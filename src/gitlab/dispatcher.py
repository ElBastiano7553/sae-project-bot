"""File d'attente entre la réception d'un webhook et l'envoi sur Discord.

GitLab attend une réponse en moins de 10 secondes et désactive un webhook qui
échoue trop souvent : le serveur répond donc tout de suite, et l'envoi sur
Discord se fait ensuite, dans une tâche de fond (``run``).

Garanties :

* un événement n'est enregistré (et donc considéré comme « déjà reçu ») que s'il
  a bien été mis en file ; sinon il est oublié pour que GitLab puisse le renvoyer ;
* la notification est enregistrée avec l'événement : si le bot s'arrête avant
  l'envoi, elle est renvoyée au redémarrage (``restore_pending``) ;
* un envoi Discord en échec est retenté (``RETRY_DELAYS``), puis marqué ``failed`` ;
  un refus définitif (``PermanentSendError`` : salon supprimé, permission manquante)
  est marqué ``failed`` tout de suite, sans bloquer la file pendant les nouveaux essais ;
* aucune erreur ne peut arrêter la tâche de fond.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from src.database.repositories import EventSummary, GitLabEventRepository, KeyValueRepository
from src.gitlab.events import Notification, format_event
from src.gitlab.rules import GitLabRules, history_rules
from src.services.content import EmbedField

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 100
# Au-delà, une notification non envoyée (bot arrêté) n'est plus renvoyée : elle serait périmée.
REPLAY_MAX_AGE_HOURS = 24
# Attente (secondes) avant chaque nouvelle tentative d'envoi sur Discord.
RETRY_DELAYS: tuple[float, ...] = (2.0, 10.0)
# Clé (kv_store) de la branche principale indiquée par le dernier webhook reçu.
DEFAULT_BRANCH_KEY = "gitlab:default_branch"


class NotificationSender(Protocol):
    async def send_notification(self, channel_id: int, notification: Notification) -> None:
        """Publie la notification ; lève une exception en cas d'échec (``PermanentSendError`` s'il est inutile de réessayer)."""


class PermanentSendError(Exception):
    """Envoi refusé pour une raison qu'un nouvel essai ne changera pas (salon supprimé, permission manquante…)."""


async def send_with_retries(
    sender: NotificationSender, channel_id: int, notification: Notification, retry_delays: Sequence[float], label: str
) -> EventStatus:
    """Envoie la notification, avec un nouvel essai après chaque attente de ``retry_delays``.

    Commun aux trois files (GitLab, GitHub, projets). Un refus définitif n'est pas retenté :
    la file étant traitée une notification à la fois, chaque essai inutile retarderait toutes
    les autres (12 secondes par notification avec les délais par défaut).
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
        retry_delays: Sequence[float] = RETRY_DELAYS,
        state: KeyValueRepository | None = None,
        on_event: Callable[[], None] | None = None,
    ) -> None:
        self.repository = repository
        self.rules = rules
        self.sender = sender
        self._bindings = bindings
        self._wait_until_ready = wait_until_ready
        self._retry_delays = tuple(retry_delays)
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_size)
        self._state = state
        self._history_rules = history_rules(rules)
        # Appelé après chaque nouvel événement enregistré (actualisation du tableau de bord).
        self._on_event = on_event

    def submit(self, event: IncomingEvent) -> SubmitStatus:
        """Valide, déduplique et met en file. Lève ``InvalidPayload`` si le JSON est incomplet."""
        notification = format_event(event.kind, event.payload, self.rules)
        # Un événement non notifié (branche non suivie, action désactivée…) reste utile à l'historique.
        record = notification or format_event(event.kind, event.payload, self._history_rules)
        if record is None:
            return SubmitStatus.IGNORED
        claimed = self.repository.claim(
            event.event_key,
            event.kind,
            notification_to_json(notification) if notification else None,
            summary_of(record),
            status=EventStatus.QUEUED if notification else EventStatus.RECORDED,
        )
        if not claimed:
            log.info("Événement GitLab déjà reçu, ignoré : %s", event.event_key)
            return SubmitStatus.DUPLICATE
        if self._state is not None and record.default_branch:
            self._state.set(DEFAULT_BRANCH_KEY, record.default_branch)
        self._notify_event()
        if notification is None:
            return SubmitStatus.RECORDED
        try:
            self._queue.put_nowait(_Job(event.event_key, notification))
        except asyncio.QueueFull:
            # On oublie l'événement pour que GitLab puisse le renvoyer plus tard.
            self.repository.release(event.event_key)
            log.warning("File des notifications GitLab pleine : événement %s refusé", event.event_key)
            return SubmitStatus.BUSY
        return SubmitStatus.QUEUED

    def _notify_event(self) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event()
        except Exception:  # le tableau de bord ne doit jamais faire échouer la réception
            log.exception("Erreur dans le rappel après un événement GitLab")

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def restore_pending(self) -> int:
        """Remet en file les notifications acceptées mais jamais envoyées (bot arrêté avant l'envoi)."""
        expired = self.repository.expire_queued(REPLAY_MAX_AGE_HOURS)
        if expired:
            log.warning("%d notification(s) GitLab trop ancienne(s) abandonnée(s)", expired)
        restored = 0
        for event_key, raw in self.repository.pending(REPLAY_MAX_AGE_HOURS):
            try:
                notification = notification_from_json(raw)
                self._queue.put_nowait(_Job(event_key, notification))
            except (TypeError, ValueError, KeyError):
                log.error("Notification GitLab %s illisible : abandonnée", event_key)
                self.repository.set_status(event_key, EventStatus.FAILED)
            except asyncio.QueueFull:
                log.error("File pleine : notification GitLab %s abandonnée", event_key)
                self.repository.set_status(event_key, EventStatus.FAILED)
            else:
                restored += 1
        if restored:
            log.info("%d notification(s) GitLab non envoyée(s) avant l'arrêt remise(s) en file", restored)
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
                log.exception("Erreur inattendue en traitant la notification GitLab %s", job.event_key)
            finally:
                self._queue.task_done()

    async def deliver(self, job: _Job) -> EventStatus:
        notification = job.notification
        route = self.rules.route_for(notification.kind)
        channel_id = self._bindings().get(route)
        if channel_id is None:
            log.warning(
                "Notification GitLab « %s » non publiée : le salon « %s » n'est pas associé (lancez /admin installer).",
                notification.kind,
                route,
            )
            status = EventStatus.SKIPPED
        else:
            status = await send_with_retries(self.sender, channel_id, job.notification, self._retry_delays, f"GitLab {job.event_key}")
        self.repository.set_status(job.event_key, status)
        return status

    async def drain(self) -> None:
        """Traite immédiatement les notifications en attente (utilisé par les tests)."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            try:
                await self.deliver(job)
            finally:
                self._queue.task_done()


def notification_to_json(notification: Notification) -> str:
    return json.dumps(asdict(notification), ensure_ascii=False)


def notification_from_json(raw: str | None) -> Notification:
    if raw is None:
        raise ValueError("notification absente")
    data = json.loads(raw)
    data["fields"] = tuple(EmbedField(**field) for field in data.get("fields", ()))
    return Notification(**data)


def summary_of(notification: Notification) -> EventSummary:
    return EventSummary(
        dedup_key=notification.dedup_key,
        action=notification.action,
        ref=notification.ref,
        title=notification.title,
        url=notification.url,
        author=notification.author,
        occurred_at=notification.timestamp,
        user_id=notification.user_id,
        user_username=notification.user_username,
        subject_id=notification.subject_id,
        subject_author_id=notification.subject_author_id,
        commit_count=notification.commit_count,
        environment=notification.environment,
    )
