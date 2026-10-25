"""Adaptateur Lavalink (vrai wavelink) face à un FAUX serveur Lavalink v4 local : aucun réseau extérieur."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from src.music.errors import BackendError
from src.music.lavalink import WavelinkBackend
from tests.music_fakes import raw_track

pytestmark = pytest.mark.integration

PASSWORD = "mot-de-passe-lavalink-0123456789"


class FakeLavalink:
    """Le strict nécessaire de l'API Lavalink v4 utilisé par wavelink pour se connecter et chercher."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.sockets: list[web.WebSocketResponse] = []
        app = web.Application()
        app.router.add_get("/v4/websocket", self.websocket)
        app.router.add_patch("/v4/sessions/{session}", self.session)
        app.router.add_get("/v4/info", self.info)
        app.router.add_get("/v4/loadtracks", self.loadtracks)
        self.runner = web.AppRunner(app)

    def _authorized(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == PASSWORD

    async def websocket(self, request: web.Request):
        if not self._authorized(request):
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"op": "ready", "resumed": False, "sessionId": "session-test"})
        async for _ in ws:
            pass
        return ws

    async def session(self, request: web.Request):
        return web.json_response({"resuming": True, "timeout": 60})

    async def info(self, request: web.Request):
        return web.json_response({"sourceManagers": ["youtube", "soundcloud"], "plugins": []})

    async def loadtracks(self, request: web.Request):
        if not self._authorized(request):
            return web.json_response({"status": 401, "error": "Unauthorized", "message": "x", "path": "/v4/loadtracks", "timestamp": 0}, status=401)
        identifier = request.query["identifier"]
        self.requests.append(identifier)
        if identifier == "scsearch:panne":
            return web.json_response({"loadType": "error", "data": {"message": "bloqué", "severity": "suspicious", "cause": "403"}})
        if identifier.startswith("https://www.youtube.com/playlist"):
            tracks = [raw_track(f"p{i}") for i in range(3)]
            return web.json_response(
                {"loadType": "playlist", "data": {"info": {"name": "Ma liste", "selectedTrack": -1}, "pluginInfo": {}, "tracks": tracks}}
            )
        if identifier.startswith("scsearch:"):
            return web.json_response({"loadType": "search", "data": [raw_track("r1", source="soundcloud"), raw_track("r2", source="soundcloud")]})
        return web.json_response({"loadType": "empty", "data": {}})

    async def start(self, port: int) -> None:
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", port).start()

    async def stop(self) -> None:
        for ws in self.sockets:
            await ws.close()
        await self.runner.cleanup()


def fake_bot():
    bot = SimpleNamespace(user=SimpleNamespace(id=123456789), dispatched=[])
    bot.dispatch = lambda event, *args: bot.dispatched.append(event)
    bot.get_guild = lambda guild_id: None
    return bot


async def wait_until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
async def lavalink(unused_tcp_port):
    server = FakeLavalink()
    await server.start(unused_tcp_port)
    yield server, unused_tcp_port
    await server.stop()


async def test_connects_searches_and_closes_cleanly(lavalink):
    server, port = lavalink
    backend = WavelinkBackend(fake_bot(), uri=f"http://127.0.0.1:{port}", password=PASSWORD, idle_timeout_seconds=300)
    backend.start()
    try:
        await wait_until(lambda: backend.ready)
        result = await backend.load("scsearch:daft punk")
        assert [t["info"]["title"] for t in result.tracks] == ["r1", "r2"] and result.playlist_name is None
        assert server.requests == ["scsearch:daft punk"]
        playlist = await backend.load("https://www.youtube.com/playlist?list=PL1")
        assert playlist.playlist_name == "Ma liste" and len(playlist.tracks) == 3
        assert (await backend.load("ytmsearch:rien")).tracks == []
        with pytest.raises(BackendError, match="suspicious"):
            await backend.load("scsearch:panne")
        with pytest.raises(BackendError):
            await backend.set_paused(1, True)  # aucun lecteur pour ce serveur
        assert backend.position_ms(1) is None
    finally:
        await backend.close()
    assert not backend.ready


async def test_wrong_password_never_marks_lavalink_ready(lavalink, caplog):
    server, port = lavalink
    backend = WavelinkBackend(fake_bot(), uri=f"http://127.0.0.1:{port}", password="mauvais-mot-de-passe-123456", idle_timeout_seconds=300)
    task = backend.start()
    try:
        await asyncio.wait_for(asyncio.shield(task), 5)
        assert not backend.ready
        assert "LAVALINK_PASSWORD" in caplog.text
        assert "mauvais-mot-de-passe" not in caplog.text  # le mot de passe n'est jamais journalisé
        with pytest.raises(BackendError):
            await backend.load("scsearch:x")
    finally:
        await backend.close()


async def test_absent_lavalink_never_blocks_and_close_cancels_the_retry_task(unused_tcp_port):
    backend = WavelinkBackend(fake_bot(), uri=f"http://127.0.0.1:{unused_tcp_port}", password=PASSWORD, idle_timeout_seconds=300)
    task = backend.start()  # retourne immédiatement : la tentative de connexion tourne en arrière-plan
    await asyncio.sleep(0.2)
    assert not backend.ready and not task.done()
    await backend.close()
    assert task.done()
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert leftovers == []


async def test_lavalink_restart_is_detected_and_reconnected(unused_tcp_port):
    """wavelink seul ne voit pas l'arrêt de Lavalink : la surveillance de l'adaptateur le détecte et se reconnecte."""
    server = FakeLavalink()
    await server.start(unused_tcp_port)
    bot = fake_bot()
    backend = WavelinkBackend(bot, uri=f"http://127.0.0.1:{unused_tcp_port}", password=PASSWORD, idle_timeout_seconds=300)
    backend.watchdog_seconds = 0.1
    backend.start()
    try:
        await wait_until(lambda: backend.ready)
        await server.stop()  # Lavalink arrêté
        await wait_until(lambda: not backend.ready)
        assert "wavelink_node_disconnected" in bot.dispatched  # le cog réinitialise alors les lecteurs
        server = FakeLavalink()
        await server.start(unused_tcp_port)  # Lavalink relancé
        await wait_until(lambda: backend.ready, timeout=20)
        assert (await backend.load("scsearch:x")).tracks
    finally:
        await backend.close()
        await server.stop()
