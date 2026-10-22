import pytest

from src.database import GitLabEventRepository
from src.gitlab.dispatcher import EventStatus, IncomingEvent, NotificationDispatcher, SubmitStatus
from src.gitlab.events import InvalidPayload
from src.gitlab.rules import GitLabRules
from src.gitlab.samples import pipeline_payload, push_payload

RULES = GitLabRules(routes={"push": "gitlab_activite", "merge_request": "gitlab_activite", "pipeline": "builds"})
BINDINGS = {"gitlab_activite": 100, "builds": 200}


class FakeSender:
    def __init__(self, fail: bool = False, fail_times: int = 0) -> None:
        self.sent: list[tuple[int, object]] = []
        self.fail = fail
        self.fail_times = fail_times
        self.attempts = 0

    async def send_notification(self, channel_id, notification):
        self.attempts += 1
        if self.fail or self.attempts <= self.fail_times:
            raise RuntimeError("Discord indisponible")
        self.sent.append((channel_id, notification))


@pytest.fixture
def repo(db):
    return GitLabEventRepository(db)


def make(repo, sender=None, bindings=BINDINGS, queue_size=10, retry_delays=()):
    return NotificationDispatcher(
        repo, RULES, sender or FakeSender(), lambda: dict(bindings), queue_size=queue_size, retry_delays=retry_delays
    )


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


async def test_transient_discord_error_is_retried(repo):
    sender = FakeSender(fail_times=1)
    dispatcher = make(repo, sender, retry_delays=(0,))
    dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload()))
    await dispatcher.drain()
    assert sender.attempts == 2
    assert len(sender.sent) == 1
    assert repo.get_status("push:uuid-1") == EventStatus.SENT


async def test_failure_after_all_retries(repo):
    sender = FakeSender(fail=True)
    dispatcher = make(repo, sender, retry_delays=(0, 0))
    dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload()))
    await dispatcher.drain()
    assert sender.attempts == 3
    assert repo.get_status("push:uuid-1") == EventStatus.FAILED
    # GitLab qui renverrait le même événement ne provoque pas de doublon.
    assert dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload())) is SubmitStatus.DUPLICATE


async def test_worker_survives_unexpected_errors(repo):
    import asyncio

    calls = {"n": 0}

    def flaky_bindings():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("base verrouillée")
        return BINDINGS

    sender = FakeSender()
    dispatcher = NotificationDispatcher(repo, RULES, sender, flaky_bindings, retry_delays=())
    task = asyncio.create_task(dispatcher.run())
    dispatcher.submit(IncomingEvent("push", "uuid-1", push_payload()))
    dispatcher.submit(IncomingEvent("push", "uuid-2", push_payload()))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done()
    assert len(sender.sent) == 1
    assert repo.get_status("push:uuid-2") == EventStatus.SENT
    task.cancel()


async def test_notifications_not_sent_before_a_stop_are_replayed(db, repo):
    stopped = make(repo)
    stopped.submit(IncomingEvent("push", "uuid-1", push_payload()))
    stopped.submit(IncomingEvent("pipeline", "uuid-2", pipeline_payload()))
    # le bot s'arrête ici : rien n'a été envoyé

    sender = FakeSender()
    restarted = make(GitLabEventRepository(db), sender)
    assert restarted.restore_pending() == 2
    await restarted.drain()
    assert [channel for channel, _ in sender.sent] == [100, 200]
    assert sender.sent[0][1].title.startswith("📤 2 commits")
    assert make(GitLabEventRepository(db)).restore_pending() == 0  # rien n'est renvoyé deux fois


async def test_old_or_corrupted_pending_notifications_are_abandoned(db, repo):
    make(repo).submit(IncomingEvent("push", "old", push_payload()))
    repo.claim("push:corrupted", "push", "{pas du json")
    db.conn.execute("UPDATE gitlab_events SET received_at = datetime('now', '-2 days') WHERE event_key = 'push:old'")
    sender = FakeSender()
    dispatcher = make(repo, sender)
    assert dispatcher.restore_pending() == 0
    assert repo.get_status("push:old") == EventStatus.FAILED
    assert repo.get_status("push:corrupted") == EventStatus.FAILED


async def test_notification_json_round_trip():
    from src.gitlab.dispatcher import notification_from_json, notification_to_json
    from src.gitlab.events import format_pipeline

    notification = format_pipeline(pipeline_payload(), RULES)
    assert notification_from_json(notification_to_json(notification)) == notification
