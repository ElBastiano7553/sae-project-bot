"""Nouveaux essais d'envoi communs aux trois files (GitLab, GitHub, projets) : un refus définitif n'est pas retenté."""

import pytest

from src.database.github import GitHubDeliveryRepository
from src.database.project_deliveries import ProjectDeliveryRepository
from src.database.projects import ProjectRepository
from src.database.repositories import GitLabEventRepository
from src.github import samples as gh
from src.github.dispatcher import GitHubDelivery, GitHubDispatcher
from src.gitlab import samples as gl
from src.gitlab.dispatcher import EventStatus, IncomingEvent, NotificationDispatcher, PermanentSendError, send_with_retries
from src.gitlab.rules import GitLabRules
from src.providers.dispatcher import ProjectDispatcher
from src.providers.translate import translate_github
from tests.test_projects import GUILD

CHANNEL = 100


class Refused(PermanentSendError):
    pass


class ScriptedSender:
    """Lève, dans l'ordre, les erreurs prévues, puis réussit."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.attempts = 0
        self.sent = []

    async def send_notification(self, channel_id, notification):
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((channel_id, notification))


@pytest.fixture
def notification():
    return translate_github("push", "d-1", gh.push_payload()).notification


async def test_definitive_refusal_stops_at_the_first_attempt(notification, caplog):
    sender = ScriptedSender(Refused("le bot ne peut pas écrire dans #code"))
    status = await send_with_retries(sender, CHANNEL, notification, (0, 0), "test")
    assert status is EventStatus.FAILED
    assert sender.attempts == 1
    assert "sans nouvel essai" in caplog.text


async def test_transient_error_is_still_retried(notification):
    sender = ScriptedSender(RuntimeError("Discord indisponible"))
    assert await send_with_retries(sender, CHANNEL, notification, (0,), "test") is EventStatus.SENT
    assert sender.attempts == 2


async def test_refusal_after_a_transient_error_stops_retrying(notification):
    sender = ScriptedSender(RuntimeError("réseau"), Refused("salon supprimé"))
    assert await send_with_retries(sender, CHANNEL, notification, (0, 0, 0), "test") is EventStatus.FAILED
    assert sender.attempts == 2


async def test_all_retries_used_on_repeated_transient_errors(notification):
    sender = ScriptedSender(*(RuntimeError("réseau") for _ in range(5)))
    assert await send_with_retries(sender, CHANNEL, notification, (0, 0), "test") is EventStatus.FAILED
    assert sender.attempts == 3


async def test_legacy_gitlab_queue_does_not_retry_a_refusal(db):
    repo = GitLabEventRepository(db)
    sender = ScriptedSender(Refused("interdit"))
    rules = GitLabRules(routes={"push": "a", "merge_request": "a", "pipeline": "a"})
    dispatcher = NotificationDispatcher(repo, rules, sender, lambda: {"a": CHANNEL}, retry_delays=(0, 0))
    event = IncomingEvent("push", "k-1", gl.push_payload())
    dispatcher.submit(event)
    await dispatcher.drain()
    assert sender.attempts == 1
    assert repo.get_status(event.event_key) == EventStatus.FAILED


async def test_legacy_github_queue_does_not_retry_a_refusal(db):
    repo = GitHubDeliveryRepository(db)
    sender = ScriptedSender(Refused("interdit"))
    dispatcher = GitHubDispatcher(repo, sender, lambda: {"gitlab_activite": CHANNEL}, retry_delays=(0, 0))
    dispatcher.submit(GitHubDelivery("push", "d-1", gh.push_payload()))
    await dispatcher.drain()
    assert sender.attempts == 1
    assert repo.get_status("push:d-1") == EventStatus.FAILED


async def test_refused_project_does_not_delay_the_next_one(db, notification):
    """Un salon refusé dans un projet ne bloque pas les autres : un seul essai, puis le suivant part."""
    projects, deliveries = ProjectRepository(db), ProjectDeliveryRepository(db)
    first = projects.create(GUILD, "github", "equipe/a", "A", created_by=1)
    second = projects.create(GUILD, "github", "equipe/b", "B", created_by=1)
    projects.set_route(GUILD, first.id, "code", 101)
    projects.set_route(GUILD, second.id, "code", 102)
    sender = ScriptedSender(Refused("interdit"))

    async def channel_guild(channel_id):
        return GUILD

    dispatcher = ProjectDispatcher(projects, deliveries, sender, channel_guild, retry_delays=(30, 30))
    event = translate_github("push", "d-1", gh.push_payload())
    dispatcher.submit(event, [first, second])
    await dispatcher.drain()  # sans la correction, attendrait 60 s (deux pauses de 30 s)
    assert sender.attempts == 2
    assert deliveries.get_status(first.id, event.delivery_key) == EventStatus.FAILED
    assert deliveries.get_status(second.id, event.delivery_key) == EventStatus.SENT
