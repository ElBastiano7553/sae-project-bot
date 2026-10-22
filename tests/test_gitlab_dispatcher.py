import pytest

from src.database import GitLabEventRepository
from src.gitlab.dispatcher import EventStatus, IncomingEvent, NotificationDispatcher, SubmitStatus
from src.gitlab.events import InvalidPayload
from src.gitlab.rules import GitLabRules
from src.gitlab.samples import pipeline_payload, push_payload

RULES = GitLabRules(routes={"push": "gitlab_activite", "merge_request": "gitlab_activite", "pipeline": "builds"})
BINDINGS = {"gitlab_activite": 100, "builds": 200}


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, object]] = []
        self.fail = fail

    async def send_notification(self, channel_id, notification):
        if self.fail:
            raise RuntimeError("Discord indisponible")
        self.sent.append((channel_id, notification))


@pytest.fixture
def repo(db):
    return GitLabEventRepository(db)


def make(repo, sender=None, bindings=BINDINGS, queue_size=10):
    return NotificationDispatcher(repo, RULES, sender or FakeSender(), lambda: dict(bindings), queue_size=queue_size)


async def test_event_is_routed_to_the_right_channel(repo):
    sender = FakeSender()
    dispatcher = make(repo, sender)
    assert dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload())) is SubmitStatus.QUEUED
    assert dispatcher.submit(IncomingEvent("pipeline", "uuid-2", pipeline_payload())) is SubmitStatus.QUEUED
    await dispatcher.drain()
    assert [channel for channel, _ in sender.sent] == [100, 200]
    assert repo.get_status("push:uuid-1") == EventStatus.SENT


async def test_same_delivery_is_notified_only_once(repo):
    sender = FakeSender()
    dispatcher = make(repo, sender)
    event = IncomingEvent("push", "uuid-1", push_payload())
    assert dispatcher.submit(event) is SubmitStatus.QUEUED
    assert dispatcher.submit(event) is SubmitStatus.DUPLICATE
    await dispatcher.drain()
    assert dispatcher.submit(event) is SubmitStatus.DUPLICATE
    assert len(sender.sent) == 1


async def test_duplicates_survive_a_restart(db, repo):
    make(repo).submit(IncomingEvent("push", "uuid-1", push_payload()))
    restarted = make(GitLabEventRepository(db))
    assert restarted.submit(IncomingEvent("push", "uuid-1", push_payload())) is SubmitStatus.DUPLICATE


async def test_ignored_event_is_not_stored(repo):
    dispatcher = make(repo)
    assert dispatcher.submit(IncomingEvent("pipeline", "uuid-1", pipeline_payload(status="running"))) is SubmitStatus.IGNORED
    assert repo.get_status("pipeline:uuid-1") is None


async def test_invalid_payload_is_not_stored(repo):
    dispatcher = make(repo)
    with pytest.raises(InvalidPayload):
        dispatcher.submit(IncomingEvent("push", "uuid-1", {"object_kind": "push"}))
    assert repo.get_status("push:uuid-1") is None


async def test_full_queue_releases_the_event_so_gitlab_can_retry(repo):
    dispatcher = make(repo, queue_size=1)
    assert dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload())) is SubmitStatus.QUEUED
    assert dispatcher.submit(IncomingEvent("push", "uuid-2", push_payload())) is SubmitStatus.BUSY
    assert repo.get_status("push:uuid-2") is None
    await dispatcher.drain()
    assert dispatcher.submit(IncomingEvent("push", "uuid-2", push_payload())) is SubmitStatus.QUEUED


async def test_unbound_channel_is_skipped_without_error(repo):
    sender = FakeSender()
    dispatcher = make(repo, sender, bindings={})
    dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload()))
    await dispatcher.drain()
    assert sender.sent == []
    assert repo.get_status("push:uuid-1") == EventStatus.SKIPPED


async def test_discord_failure_is_recorded_and_does_not_stop_the_worker(repo):
    dispatcher = make(repo, FakeSender(fail=True))
    dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload()))
    dispatcher.submit(IncomingEvent("pipeline", "uuid-2", pipeline_payload()))
    await dispatcher.drain()
    assert repo.get_status("push:uuid-1") == EventStatus.FAILED
    assert repo.get_status("pipeline:uuid-2") == EventStatus.FAILED


async def test_run_waits_until_ready_then_delivers(repo):
    import asyncio

    ready = asyncio.Event()
    sender = FakeSender()
    dispatcher = NotificationDispatcher(repo, RULES, sender, lambda: BINDINGS, wait_until_ready=ready.wait)
    task = asyncio.create_task(dispatcher.run())
    dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload()))
    await asyncio.sleep(0)
    assert sender.sent == []
    ready.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(sender.sent) == 1
    task.cancel()
