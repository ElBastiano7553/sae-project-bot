"""Faux moteur audio pour les tests de la musique (aucun Lavalink, aucun réseau)."""

from __future__ import annotations

import asyncio
import itertools

from src.music.errors import BackendError
from src.music.permissions import Listener
from src.music.service import LoadResult, MusicConfig, MusicService
from src.database.music import MusicRepository

_ids = itertools.count(1)

GUILD_A, GUILD_B = 1001, 2002
VOICE_A, VOICE_A2, VOICE_B = 11, 12, 21
TEXT_A = 31


def raw_track(title: str = "Titre", *, source: str = "youtube", length: int = 200_000, identifier: str | None = None,
              uri: str | None = "auto", stream: bool = False, author: str = "Artiste") -> dict:
    identifier = identifier or f"id{next(_ids)}"
    if uri == "auto":
        uri = f"https://www.youtube.com/watch?v={identifier}" if source == "youtube" else f"https://soundcloud.com/a/{identifier}"
    return {
        "encoded": f"ENC-{identifier}",
        "info": {
            "identifier": identifier,
            "isSeekable": True,
            "author": author,
            "length": length,
            "isStream": stream,
            "position": 0,
            "title": title,
            "uri": uri,
            "artworkUrl": None,
            "isrc": None,
            "sourceName": source,
        },
        "pluginInfo": {},
        "userData": {},
    }


class FakeBackend:
    """Enregistre les appels ; ``results`` associe un identifiant (« ytmsearch:… » ou lien) à un ``LoadResult``."""

    def __init__(self) -> None:
        self.ready = True
        self.results: dict[str, LoadResult | Exception] = {}
        self.calls: list[tuple] = []
        self.connected: dict[int, int] = {}
        self.playing: dict[int, tuple[str, int]] = {}  # guild → (identifiant de la piste, entrée)
        self.paused: dict[int, bool] = {}
        self.volumes: dict[int, int] = {}
        self.fail_play: set[str] = set()  # identifiants dont la lecture échoue
        self.hang: set[str] = set()  # opérations qui ne répondent jamais
        self.positions: dict[int, int] = {}
        self.info = None  # LavalinkInfo annoncée par Lavalink (None : pas encore lue)
        self.live: dict[int, object] = {}  # guild → LiveVoiceState renvoyé par live_voice_state

    async def _maybe_hang(self, what: str) -> None:
        if what in self.hang:
            await asyncio.Event().wait()

    async def load(self, identifier: str) -> LoadResult:
        self.calls.append(("load", identifier))
        await self._maybe_hang("load")
        result = self.results.get(identifier, LoadResult([]))
        if isinstance(result, Exception):
            raise result
        return result

    async def live_voice_state(self, guild_id: int):
        from src.music.service import LiveVoiceState

        self.calls.append(("live", guild_id))
        return self.live.get(guild_id, LiveVoiceState(exists=guild_id in self.connected))

    async def connect(self, guild_id: int, channel_id: int) -> None:
        self.calls.append(("connect", guild_id, channel_id))
        await self._maybe_hang("connect")
        self.connected[guild_id] = channel_id

    async def play(self, guild_id: int, track, *, entry_id: int, volume: int) -> None:
        self.calls.append(("play", guild_id, track.identifier, entry_id, volume))
        await self._maybe_hang("play")
        if track.identifier in self.fail_play:
            raise BackendError("lecture refusée")
        self.playing[guild_id] = (track.identifier, entry_id)
        self.volumes[guild_id] = volume

    async def set_paused(self, guild_id: int, paused: bool) -> None:
        self.calls.append(("pause", guild_id, paused))
        self.paused[guild_id] = paused

    async def stop(self, guild_id: int) -> None:
        self.calls.append(("stop", guild_id))
        self.playing.pop(guild_id, None)

    async def set_volume(self, guild_id: int, volume: int) -> None:
        self.calls.append(("volume", guild_id, volume))
        await self._maybe_hang("volume")
        self.volumes[guild_id] = volume

    async def disconnect(self, guild_id: int) -> None:
        self.calls.append(("disconnect", guild_id))
        self.connected.pop(guild_id, None)
        self.playing.pop(guild_id, None)

    def position_ms(self, guild_id: int) -> int | None:
        return self.positions.get(guild_id)

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)


def make_service(db, backend: FakeBackend | None = None, **config) -> tuple[MusicService, FakeBackend, list[int]]:
    backend = backend or FakeBackend()
    changes: list[int] = []
    options = {"sources": ("youtubemusic", "soundcloud"), **config}
    service = MusicService(backend, MusicRepository(db), MusicConfig(**options), on_change=changes.append, clock=lambda: 0.0)
    service._clock = _Clock()  # anti-spam : chaque appel avance de 10 s (sauf test dédié)
    return service, backend, changes


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 10.0
        return self.now


def listener(user_id: int = 1, *, voice: int | None = VOICE_A, admin: bool = False, dj: bool = False, humans: int = 2, name: str = "Alice") -> Listener:
    return Listener(user_id=user_id, name=name, voice_channel_id=voice, is_admin=admin, has_dj_role=dj, humans_in_channel=humans)
