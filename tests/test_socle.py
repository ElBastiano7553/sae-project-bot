"""Socle 1.15.0 : file de notifications commune et corrections des points ouverts de l'audit d'octobre.

* arrêt propre : ``close()`` laisse finir l'envoi en cours (sinon il serait renvoyé en double) ;
* une erreur passagère de Discord en vérifiant le salon d'un projet n'abandonne plus la notification ;
* la remise en file au démarrage a lieu avant l'ouverture du port ;
* une limite de débit GitHub n'est plus présentée comme un refus d'accès.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.database.project_deliveries import ProjectDeliveryRepository
from src.database.projects import ProjectRepository
from src.discord_bot import bot as bot_module
from src.discord_bot.bot import SaeBot, _stop_queue
from src.gitlab import dispatcher as gitlab_dispatcher
from src.gitlab.events import Notification
from src.notifications import EventStatus, NotificationQueue, send_with_retries
from src.providers.dispatcher import ChannelLookupError, ProjectDispatcher
from src.providers.github_app import GitHubAccessError, GitHubUnavailable, GitHubUserClient, is_rate_limited
from src.providers.translate import ProjectEvent
from tests.test_projects import GUILD

CHANNEL = 101


def _notification() -> Notification:
    return Notification(kind="push", title="Nouveau commit", description="", color=0, dedup_key="push:1")


# --- File commune -------------------------------------------------------------------------------------


class SlowQueue(NotificationQueue[str]):
    label = "test"

    def __init__(self) -> None:
        super().__init__(wait_until_ready=None, queue_size=10, retry_delays=())
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.done: list[str] = []

    def submit(self, job: str) -> None:
        self._queue.put_nowait(job)

    async def deliver(self, job: str) -> EventStatus:
        self.started.set()
        await self.release.wait()
        self.done.append(job)
        return EventStatus.SENT


def test_the_old_module_still_exports_the_shared_pieces():
    # Le code et les tests qui importent depuis src.gitlab.dispatcher continuent de fonctionner.
    assert gitlab_dispatcher.send_with_retries is send_with_retries
    assert gitlab_dispatcher.EventStatus is EventStatus


async def test_stop_lets_the_current_send_finish():
    queue = SlowQueue()
    queue.submit("a")
    queue.submit("b")
    task = asyncio.create_task(queue.run())
    await queue.started.wait()
    stopping = asyncio.create_task(queue.stop(task, timeout=5))
    await asyncio.sleep(0.1)
    assert not stopping.done()  # l'envoi de « a » n'est pas coupé
    queue.release.set()
    await stopping
    assert queue.done == ["a"]  # « b » reste en file (et donc « queued » en base, renvoyé au redémarrage)
    assert task.done() and queue.pending == 1


async def test_stop_interrupts_a_send_that_takes_too_long(caplog):
    queue = SlowQueue()
    queue.submit("a")
    task = asyncio.create_task(queue.run())
    await queue.started.wait()
    await queue.stop(task, timeout=0.1)
    assert task.cancelled() and queue.done == []
    assert "interrompu" in caplog.text


async def test_stop_of_an_idle_queue_is_immediate():
    queue = SlowQueue()
    task = asyncio.create_task(queue.run())
    await asyncio.sleep(0)
    await asyncio.wait_for(queue.stop(task, timeout=5), timeout=1)
    assert task.done()


async def test_the_background_task_survives_an_unexpected_error(caplog):
    class Broken(SlowQueue):
        async def deliver(self, job):
            if job == "boom":
                raise RuntimeError("panne")
            self.done.append(job)
            return EventStatus.SENT

    queue = Broken()
    queue.submit("boom")
    queue.submit("ok")
    task = asyncio.create_task(queue.run())
    await asyncio.wait_for(queue._queue.join(), timeout=1)
    assert queue.done == ["ok"] and not task.done()
    assert "Erreur inattendue" in caplog.text
    await queue.stop(task)


async def test_stop_queue_without_queue_only_cancels_the_task():
    task = asyncio.create_task(asyncio.sleep(60))
    await _stop_queue(None, task)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    await _stop_queue(None, None)  # rien à arrêter


# --- Vérification du salon d'un projet ---------------------------------------------------------------


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_notification(self, channel_id, notification):
        self.sent.append(channel_id)


@pytest.fixture
def project_world(db):
    projects, deliveries = ProjectRepository(db), ProjectDeliveryRepository(db)
    project = projects.create(GUILD, "github", "ElBastiano7553/game-project", "Jeu", created_by=1)
    projects.set_route(GUILD, project.id, "code", CHANNEL)
    return projects, deliveries, project


def _event() -> ProjectEvent:
    return ProjectEvent(
        provider="github", event="push", delivery_id="abc", category="code", value=None, branch=None, notification=_notification()
    )


async def test_a_transient_discord_error_is_retried_before_sending(project_world):
    projects, deliveries, project = project_world
    calls = []

    async def channel_guild(channel_id):
        calls.append(channel_id)
        if len(calls) == 1:
            raise ChannelLookupError("HTTP 503")
        return GUILD

    sender = FakeSender()
    dispatcher = ProjectDispatcher(projects, deliveries, sender, channel_guild, retry_delays=(0,))
    dispatcher.submit(_event(), [project])
    await dispatcher.drain()
    assert sender.sent == [CHANNEL] and len(calls) == 2


async def test_a_lasting_discord_error_marks_the_notification_failed_not_skipped(project_world):
    projects, deliveries, project = project_world

    async def channel_guild(channel_id):
        raise ChannelLookupError("HTTP 503")

    sender = FakeSender()
    dispatcher = ProjectDispatcher(projects, deliveries, sender, channel_guild, retry_delays=(0,))
    dispatcher.submit(_event(), [project])
    job = dispatcher._queue.get_nowait()
    assert await dispatcher.deliver(job) is EventStatus.FAILED
    dispatcher._queue.task_done()
    assert sender.sent == []


def _http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "erreur")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_http_error(discord.NotFound, 404), None),
        (_http_error(discord.Forbidden, 403), None),
        (_http_error(discord.DiscordServerError, 503), ChannelLookupError),
        (_http_error(discord.HTTPException, 429), ChannelLookupError),
    ],
)
async def test_channel_guild_id_tells_missing_from_unavailable(error, expected):
    async def fetch_channel(channel_id):
        raise error

    fake = SimpleNamespace(get_channel=lambda _: None, fetch_channel=fetch_channel)
    if expected is None:
        assert await SaeBot.channel_guild_id(fake, CHANNEL) is None
    else:
        with pytest.raises(expected):
            await SaeBot.channel_guild_id(fake, CHANNEL)


def test_pending_notifications_are_restored_before_the_port_opens():
    # Sinon un webhook reçu entre l'ouverture du port et la remise en file serait envoyé deux fois.
    for method in (SaeBot._start_gitlab_webhooks, SaeBot._start_github_webhooks, SaeBot.start_project_reception):
        source = inspect.getsource(method)
        assert source.index("restore_pending()") < source.index(".start()"), method.__name__
    assert "await server.stop()" in inspect.getsource(bot_module.SaeBot.close)


# --- Limite de débit GitHub ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "limited"),
    [
        (429, {}, True),
        (403, {"x-ratelimit-remaining": "0"}, True),
        (403, {"retry-after": "60"}, True),
        (403, {"x-ratelimit-remaining": "4999"}, False),
        (401, {"x-ratelimit-remaining": "0"}, False),
        (200, {}, False),
    ],
)
def test_rate_limit_detection(status, headers, limited):
    assert is_rate_limited(status, headers) is limited


async def _github(handler):
    app = web.Application()
    app.router.add_get("/user/installations", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_a_github_rate_limit_is_reported_as_unavailable_not_refused():
    async def limited(request):
        return web.json_response({"message": "API rate limit exceeded"}, status=403, headers={"X-RateLimit-Remaining": "0"})

    server = await _github(limited)
    try:
        client = GitHubUserClient("Iv1.0123456789", "secret", api_url=str(server.make_url("")))
        with pytest.raises(GitHubUnavailable, match="limite de requêtes"):
            await client.installation_access("jeton", 1)
    finally:
        await server.close()


async def test_a_real_github_refusal_is_still_an_access_error():
    async def refused(request):
        return web.json_response({"message": "Forbidden"}, status=403, headers={"X-RateLimit-Remaining": "4000"})

    server = await _github(refused)
    try:
        client = GitHubUserClient("Iv1.0123456789", "secret", api_url=str(server.make_url("")))
        with pytest.raises(GitHubAccessError):
            await client.installation_access("jeton", 1)
    finally:
        await server.close()
