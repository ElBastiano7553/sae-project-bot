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
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from src.database.repositories import EventSummary, GitLabEventRepository, KeyValueRepository
from src.gitlab.events import Notification, format_event
from src.gitlab.rules import GitLabRules, history_rules

# Socle commun des files (déplacé dans src/notifications.py en 1.15.0), réexporté ici pour les
# modules et les tests qui l'importent depuis ce fichier.
from src.notifications import (  # noqa: F401
    DEFAULT_QUEUE_SIZE,
    REPLAY_MAX_AGE_HOURS,
    RETRY_DELAYS,
    EventStatus,
    NotificationQueue,
    NotificationSender,
    PermanentSendError,
    SubmitStatus,
    notification_from_json,
    notification_to_json,
    send_with_retries,
)

log = logging.getLogger(__name__)

# Clé (kv_store) de la branche principale indiquée par le dernier webhook reçu.
DEFAULT_BRANCH_KEY = "gitlab:default_branch"


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


class NotificationDispatcher(NotificationQueue[_Job]):
    label = "GitLab"

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
        super().__init__(wait_until_ready=wait_until_ready, queue_size=queue_size, retry_delays=retry_delays)
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

    def describe(self, job: _Job) -> str:
        return f"GitLab {job.event_key}"

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
