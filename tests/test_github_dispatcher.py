"""File d'attente GitHub : déduplication, reprise après arrêt, erreurs d'envoi."""

import asyncio

import pytest

from src.database.github import GitHubDeliveryRepository
from src.github.dispatcher import GitHubDelivery, GitHubDispatcher
from src.github.samples import issue_payload, pull_request_payload, push_payload
from src.gitlab.dispatcher import EventStatus, SubmitStatus
from src.gitlab.events import InvalidPayload

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
    return GitHubDeliveryRepository(db)


def make(repo, sender=None, bindings=BINDINGS, queue_size=10, retry_delays=()):
    return GitHubDispatcher(repo, sender or FakeSender(), lambda: dict(bindings), queue_size=queue_size, retry_delays=retry_delays)


def delivery(event="push", payload=None, delivery_id="d-1"):
    return GitHubDelivery(event=event, delivery_id=delivery_id, payload=payload if payload is not None else push_payload())


async def test_notification_goes_to_gitlab_activite(repo):
    sender = FakeSender()
    dispatcher = make(repo, sender)
    assert dispatcher.submit(delivery()) is SubmitStatus.QUEUED
    await dispatcher.drain()
    assert [channel for channel, _ in sender.sent] == [100]
    assert repo.get_status("push:d-1") == EventStatus.SENT


async def test_same_delivery_is_notified_once_even_after_restart(db, repo):
    dispatcher = make(repo)
    assert dispatcher.submit(delivery()) is SubmitStatus.QUEUED
    restarted = make(GitHubDeliveryRepository(db))
    assert restarted.submit(delivery()) is SubmitStatus.DUPLICATE


async def test_same_fact_with_other_delivery_id_is_a_duplicate(repo):
    dispatcher = make(repo)
    assert dispatcher.submit(delivery(delivery_id="a")) is SubmitStatus.QUEUED
    assert dispatcher.submit(delivery(delivery_id="b")) is SubmitStatus.DUPLICATE


async def test_ignored_event_is_not_stored(repo):
    payload = pull_request_payload()
    payload["action"] = "synchronize"
    assert make(repo).submit(delivery("pull_request", payload)) is SubmitStatus.IGNORED
    assert repo.last_received_at() is None


async def test_invalid_payload_raises_and_is_not_stored(repo):
    with pytest.raises(InvalidPayload):
        make(repo).submit(delivery("issues", {"action": "opened", "repository": {"full_name": "x"}}))
    assert repo.last_received_at() is None


async def test_full_queue_releases_the_delivery(repo):
    dispatcher = make(repo, queue_size=1)
    assert dispatcher.submit(delivery(delivery_id="a")) is SubmitStatus.QUEUED
    assert dispatcher.submit(delivery("issues", issue_payload(), delivery_id="b")) is SubmitStatus.BUSY
    assert repo.get_status("issues:b") is None


async def test_unbound_channel_is_skipped(repo):
    sender = FakeSender()
    dispatcher = make(repo, sender, bindings={})
    dispatcher.submit(delivery())
    await dispatcher.drain()
    assert sender.sent == []
    assert repo.get_status("push:d-1") == EventStatus.SKIPPED


async def test_transient_discord_error_is_retried(repo):
    sender = FakeSender(fail_times=1)
    dispatcher = make(repo, sender, retry_delays=(0,))
    dispatcher.submit(delivery())
    await dispatcher.drain()
    assert len(sender.sent) == 1
    assert repo.get_status("push:d-1") == EventStatus.SENT


async def test_failure_after_all_retries_is_recorded(repo):
    sender = FakeSender(fail=True)
    dispatcher = make(repo, sender, retry_delays=(0, 0))
    dispatcher.submit(delivery())
    await dispatcher.drain()
    assert sender.attempts == 3
    assert repo.get_status("push:d-1") == EventStatus.FAILED


async def test_worker_survives_errors_and_keeps_sending(repo):
    sender = FakeSender(fail_times=1)
    dispatcher = make(repo, sender)
    dispatcher.submit(delivery(delivery_id="a"))
    dispatcher.submit(delivery("issues", issue_payload(), delivery_id="b"))
    task = asyncio.create_task(dispatcher.run())
    try:
        for _ in range(50):
            if repo.get_status("issues:b") == EventStatus.SENT:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert repo.get_status("push:a") == EventStatus.FAILED
    assert repo.get_status("issues:b") == EventStatus.SENT
    assert not task.done() or task.cancelled()


async def test_run_waits_until_ready(repo):
    ready = asyncio.Event()
    sender = FakeSender()
    dispatcher = GitHubDispatcher(repo, sender, lambda: BINDINGS, wait_until_ready=ready.wait, retry_delays=())
    dispatcher.submit(delivery())
    task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(0.02)
    assert sender.sent == []
    ready.set()
    for _ in range(50):
        if sender.sent:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert len(sender.sent) == 1


async def test_pending_notifications_are_replayed_after_restart(db, repo):
    make(repo).submit(delivery())  # accepté, jamais envoyé (arrêt du bot)
    sender = FakeSender()
    restarted = make(GitHubDeliveryRepository(db), sender)
    assert restarted.restore_pending() == 1
    await restarted.drain()
    assert len(sender.sent) == 1
    assert sender.sent[0][1].title.startswith("📤 2 commits")


async def test_old_or_corrupted_pending_notifications_are_abandoned(db, repo):
    make(repo).submit(delivery(delivery_id="old"))
    make(repo).submit(delivery("issues", issue_payload(), delivery_id="bad"))
    db.conn.execute("UPDATE github_deliveries SET received_at = datetime('now', '-2 days') WHERE delivery_key = 'push:old'")
    db.conn.execute("UPDATE github_deliveries SET notification = '{oops' WHERE delivery_key = 'issues:bad'")
    db.conn.commit()
    assert make(repo).restore_pending() == 0
    assert repo.get_status("push:old") == EventStatus.FAILED
    assert repo.get_status("issues:bad") == EventStatus.FAILED


async def test_github_deliveries_do_not_touch_gitlab_history(db, repo):
    make(repo).submit(delivery())
    assert db.conn.execute("SELECT COUNT(*) FROM gitlab_events").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM github_deliveries").fetchone()[0] == 1


async def test_purge_older_than(db, repo):
    make(repo).submit(delivery())
    db.conn.execute("UPDATE github_deliveries SET received_at = datetime('now', '-400 days')")
    db.conn.commit()
    assert repo.purge_older_than(365) == 1
    assert repo.last_received_at() is None
