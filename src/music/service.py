"""Orchestration de la musique : recherche, file, lecture, volume, historique et favoris.

Le service ne connaît ni Discord ni wavelink : il reçoit des ``Listener`` (la personne qui
agit, décrite par le cog) et parle au moteur audio à travers ``AudioBackend``. Les tests
utilisent un faux moteur ; en production, c'est ``lavalink.WavelinkBackend``.

Concurrence : toute opération qui modifie un lecteur prend ``GuildPlayer.lock``. Une fin de
piste, un /skip et une erreur de lecture arrivés en même temps sont donc traités
l'un après l'autre, et chaque événement de fin porte l'identifiant de l'entrée concernée :
un événement qui ne correspond plus au titre en cours est ignoré (jamais deux « suivant »).

Après chaque changement visible, ``on_change(guild_id)`` est appelé (le cog actualise le panneau).

Les commandes slash ET les boutons du panneau appellent les mêmes méthodes : une seule
implémentation de la lecture, de la file et des permissions.

Le chemin du son est suivi étape par étape dans ``self.audio`` (``diagnostics.AudioMonitor``) :
recherche, piste chargée, lecteur démarré, connexion vocale, audio envoyé (commande /audiocheck).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from src.database.music import FavoriteRow, FavoritesFull, HistoryRow, MusicRepository
from src.music.errors import (
    BackendError,
    InvalidQuery,
    MusicError,
    MusicUnavailable,
    NoResults,
    NothingPlaying,
    QueueFull,
    SlowDown,
    SourceUnavailable,
    StaleControl,
)
from src.music.diagnostics import AudioMonitor, AudioReport, LavalinkInfo
from src.music.models import QueueEntry, SearchOutcome, Track
from src.music.permissions import Listener, MusicPolicy
from src.music.player import LOOP_LABELS, GuildPlayer, LoopMode, PlayerManager, PlayerState
from src.music.search import SEARCH_CHOICES, SOURCES, normalize_track, prepare_query, validate_url

log = logging.getLogger(__name__)

MIN_VOLUME, MAX_VOLUME = 0, 100
# Délai maximal d'une recherche et d'une commande envoyée au moteur audio.
SEARCH_TIMEOUT_SECONDS = 12.0
COMMAND_TIMEOUT_SECONDS = 8.0
# Après ce nombre d'échecs de lecture d'affilée, la lecture s'arrête (évite de vider une file de
# pistes illisibles en boucle quand une source est bloquée).
MAX_CONSECUTIVE_FAILURES = 3
# Délai minimal entre deux recherches d'une même personne (anti-spam).
SEARCH_COOLDOWN_SECONDS = 3.0
# Recherches simultanées au plus par serveur.
MAX_PARALLEL_SEARCHES = 2
# Raisons de fin de piste de Lavalink qui font passer au titre suivant. « stopped » et « replaced »
# viennent d'une action du bot (déjà traitée) ; « cleanup » signale un lecteur détruit.
ADVANCE_REASONS = frozenset({"finished", "loadFailed"})


@dataclass(frozen=True)
class LoadResult:
    """Réponse brute du moteur audio à une recherche ou un lien."""

    tracks: Sequence[dict]  # pistes au format Lavalink v4
    playlist_name: str | None = None


@dataclass(frozen=True)
class LiveVoiceState:
    """État d'un lecteur tel que Lavalink le décrit à l'instant (aucun jeton, aucune session vocale)."""

    exists: bool
    connected: bool = False
    ping_ms: int = -1
    position_ms: int = 0
    paused: bool = False
    has_track: bool = False
    volume: int = 0
    voice_sent: bool = False  # session, serveur et jeton vocaux transmis par le bot
    channel_sent: bool = False  # identifiant du salon transmis (exigé par DAVE depuis Lavalink 4.2)


class AudioBackend(Protocol):
    """Ce que le service attend du moteur audio (Lavalink en production)."""

    info: LavalinkInfo | None  # version et sources de Lavalink, connues après la connexion

    @property
    def ready(self) -> bool: ...

    async def live_voice_state(self, guild_id: int) -> LiveVoiceState | None: ...

    async def load(self, identifier: str) -> LoadResult: ...

    async def connect(self, guild_id: int, channel_id: int) -> None: ...

    async def play(self, guild_id: int, track: Track, *, entry_id: int, volume: int) -> None: ...

    async def set_paused(self, guild_id: int, paused: bool) -> None: ...

    async def stop(self, guild_id: int) -> None: ...

    async def set_volume(self, guild_id: int, volume: int) -> None: ...

    async def disconnect(self, guild_id: int) -> None: ...

    def position_ms(self, guild_id: int) -> int | None: ...


@dataclass(frozen=True)
class MusicConfig:
    sources: tuple[str, ...]
    default_volume: int = 50
    max_queue: int = 100
    max_playlist: int = 25
    max_track_minutes: int = 180  # 0 = pas de limite
    dj_role_configured: bool = False


@dataclass(frozen=True)
class EnqueueResult:
    added: tuple[QueueEntry, ...]
    first_position: int  # position dans la file du premier titre ajouté (0 = lancé tout de suite)
    started: QueueEntry | None  # titre lancé par cet ajout, s'il y en a un
    not_added: int = 0  # titres refusés faute de place
    connected: bool = False  # le bot vient de rejoindre le salon


@dataclass(frozen=True)
class Snapshot:
    """État d'un lecteur à un instant, pour l'affichage (copie : sans risque si le lecteur change)."""

    guild_id: int
    state: PlayerState
    session_id: str
    voice_channel_id: int | None
    current: QueueEntry | None
    position_ms: int | None
    upcoming: tuple[QueueEntry, ...]
    volume: int
    last_problem: str | None
    queue_length_ms: int
    loop: LoopMode = LoopMode.OFF

    @property
    def paused(self) -> bool:
        return self.state is PlayerState.PAUSED


class MusicService:
    def __init__(
        self,
        backend: AudioBackend,
        repo: MusicRepository,
        config: MusicConfig,
        *,
        on_change: Callable[[int], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        audio_clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.backend = backend
        self.repo = repo
        self.config = config
        self.policy = MusicPolicy(dj_role_configured=config.dj_role_configured)
        self.players = PlayerManager(max_queue=config.max_queue, default_volume=config.default_volume)
        self.on_change = on_change
        self._clock = clock
        self._last_search: dict[tuple[int, int], float] = {}
        self._search_slots: dict[int, asyncio.Semaphore] = {}
        self._rng = rng or random.Random()
        self.audio = AudioMonitor(audio_clock)

    # --- Outils ---------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.backend.ready

    def require_available(self) -> None:
        if not self.backend.ready:
            raise MusicUnavailable(
                "Le moteur audio (Lavalink) n'est pas joignable pour le moment. Réessayez dans quelques minutes ; "
                "si cela dure, prévenez un administrateur (/etat)."
            )

    def _player(self, guild_id: int) -> GuildPlayer:
        stored = None
        if self.players.get(guild_id) is None:
            try:
                stored = self.repo.volume(guild_id)
            except Exception:
                log.exception("Volume enregistré illisible pour le serveur %s", guild_id)
        return self.players.get_or_create(guild_id, volume=stored)

    def _notify(self, guild_id: int) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(guild_id)
        except Exception:
            log.exception("Actualisation du panneau de musique impossible (serveur %s)", guild_id)

    async def _call(self, coro, what: str):
        """Commande au moteur audio, avec délai maximal. Toute erreur devient ``SourceUnavailable``."""
        try:
            return await asyncio.wait_for(coro, COMMAND_TIMEOUT_SECONDS)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("Moteur audio : délai dépassé pendant « %s »", what)
            raise SourceUnavailable("Le moteur audio ne répond pas assez vite. Réessayez dans un instant.") from None
        except BackendError as exc:
            log.warning("Moteur audio : échec pendant « %s » : %s", what, exc)
            raise SourceUnavailable("Le moteur audio a refusé l'opération. Réessayez dans un instant.") from None

    def snapshot(self, guild_id: int) -> Snapshot:
        player = self.players.get(guild_id)
        if player is None:
            return Snapshot(guild_id, PlayerState.DISCONNECTED, "", None, None, None, (), self.config.default_volume, None, 0)
        position = None
        if player.current is not None:
            try:
                position = self.backend.position_ms(guild_id)
            except Exception:
                position = None
        return Snapshot(
            guild_id=guild_id,
            state=player.state,
            session_id=player.session_id,
            voice_channel_id=player.voice_channel_id,
            current=player.current,
            position_ms=position,
            upcoming=tuple(player.queue),
            volume=player.volume,
            last_problem=player.last_problem,
            queue_length_ms=player.queue.total_length_ms(),
            loop=player.loop,
        )

    # --- Recherche --------------------------------------------------------------------

    async def search(self, guild_id: int, user_id: int, query: str, *, source: str | None = None) -> SearchOutcome:
        """Cherche des pistes (texte) ou résout un lien. Lève une ``MusicError`` claire en cas d'échec."""
        self.require_available()
        prepared = prepare_query(query, self.config.sources, source=source)
        now = self._clock()
        last = self._last_search.get((guild_id, user_id))
        if last is not None and now - last < SEARCH_COOLDOWN_SECONDS:
            raise SlowDown("Une recherche à la fois : patientez quelques secondes avant la suivante.")
        self._last_search[(guild_id, user_id)] = now
        slots = self._search_slots.setdefault(guild_id, asyncio.Semaphore(MAX_PARALLEL_SEARCHES))
        if slots.locked():
            raise SlowDown("Plusieurs recherches sont déjà en cours sur ce serveur : réessayez dans un instant.")
        async with slots:
            failures = 0
            for identifier, key in zip(prepared.identifiers, prepared.sources):
                try:
                    result = await asyncio.wait_for(self.backend.load(identifier), SEARCH_TIMEOUT_SECONDS)
                except (TimeoutError, asyncio.TimeoutError):
                    log.warning("Recherche musicale : délai dépassé (source %s)", key)
                    failures += 1
                    continue
                except BackendError as exc:
                    log.warning("Recherche musicale : la source %s a échoué : %s", key, exc)
                    failures += 1
                    continue
                outcome = self._build_outcome(result, prepared.kind, key)
                if outcome.tracks:
                    self.audio.search_ok(guild_id, key)
                    log.info("Musique [serveur %s] étape 1/5 : recherche réussie (%d titre(s), source %s)", guild_id, len(outcome.tracks), key)
                    return outcome
                if outcome.rejected:
                    raise NoResults(self._rejection_text(outcome.rejected))
        if failures == len(prepared.identifiers):
            raise SourceUnavailable(
                "La recherche a échoué : la source musicale ne répond pas ou bloque le serveur. Réessayez plus tard "
                "ou essayez une autre source."
            )
        if prepared.kind == "url":
            raise NoResults("Rien de lisible à ce lien (titre supprimé, privé ou indisponible dans la région du serveur).")
        raise NoResults(f"Aucun titre trouvé pour « {prepared.display} ».")

    def _rejection_text(self, count: int) -> str:
        limit = self.config.max_track_minutes
        why = f"plus de {limit} minutes ou direct" if limit else "direct"
        return f"{count} titre(s) trouvé(s), mais aucun n'est accepté ({why})."

    def _build_outcome(self, result: LoadResult, kind: str, source_key: str) -> SearchOutcome:
        limit_ms = self.config.max_track_minutes * 60_000
        accepted: list[Track] = []
        rejected = 0
        raw_tracks = list(result.tracks or ())
        playlist = result.playlist_name is not None
        cap = self.config.max_playlist if playlist else (SEARCH_CHOICES if kind == "search" else 1)
        for data in raw_tracks:
            if len(accepted) >= cap:
                break
            track = normalize_track(data)
            if track is None or track.is_stream or (limit_ms and track.length_ms > limit_ms):
                rejected += 1
                continue
            accepted.append(track)
        return SearchOutcome(
            tracks=tuple(accepted),
            query_kind="playlist" if playlist else ("search" if kind == "search" else "track"),
            source_key=source_key,
            playlist_name=result.playlist_name,
            playlist_total=len(raw_tracks) if playlist else 0,
            rejected=rejected,
        )

    async def resolve_uri(self, guild_id: int, user_id: int, uri: str) -> Track:
        """Retrouve une piste d'après son lien (favori, historique). Le lien est revalidé comme une saisie."""
        validate_url(uri, self.config.sources)
        outcome = await self.search(guild_id, user_id, uri)
        return outcome.tracks[0]

    # --- File et lecture -----------------------------------------------------------------

    async def enqueue(
        self,
        guild_id: int,
        listener: Listener,
        tracks: Sequence[Track],
        *,
        text_channel_id: int | None = None,
    ) -> EnqueueResult:
        """Ajoute des pistes à la file (connecte le bot si besoin) et lance la lecture si rien ne joue."""
        if not tracks:
            raise NoResults("Aucun titre à ajouter.")
        self.require_available()
        player = self._player(guild_id)
        async with player.lock:
            self.policy.require_same_channel(player, listener)
            entries = [QueueEntry(track, listener.user_id, listener.name) for track in tracks]
            if player.queue.remaining <= 0:
                raise QueueFull(f"La file est pleine ({player.queue.max_size} titres au plus). Attendez qu'elle avance un peu.")
            connected = False
            if not player.connected:
                await self._call(self.backend.connect(guild_id, listener.voice_channel_id), "connexion au salon vocal")
                player.connected_to(listener.voice_channel_id)
                connected = True
            if text_channel_id is not None:
                player.text_channel_id = text_channel_id
            added = player.queue.extend(entries)
            started = await self._play_next(player) if player.state is PlayerState.IDLE else None
            leftovers = entries[len(added):]
            if leftovers and player.queue.remaining > 0:  # le titre lancé a libéré une place
                added += player.queue.extend(leftovers)
            if started is not None and started.entry_id == added[0].entry_id:
                first_position = 0
            else:
                first_position = player.queue.position_of(added[0].entry_id) or 0
        self._notify(guild_id)
        return EnqueueResult(tuple(added), first_position, started, len(entries) - len(added), connected)

    async def _play_next(self, player: GuildPlayer) -> QueueEntry | None:
        """Lance la piste suivante (verrou déjà pris). Passe les pistes illisibles ; s'arrête après trop d'échecs."""
        while True:
            entry = player.queue.pop_next()
            if entry is None:
                player.finished()
                return None
            try:
                await self._call(
                    self.backend.play(player.guild_id, entry.track, entry_id=entry.entry_id, volume=player.volume),
                    "lecture",
                )
            except MusicError:
                player.consecutive_failures += 1
                player.last_problem = f"« {entry.track.title} » n'a pas pu être lu et a été passé."
                if player.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    cleared = player.queue.clear()
                    player.finished()
                    player.consecutive_failures = 0
                    player.last_problem = (
                        f"Lecture arrêtée après {MAX_CONSECUTIVE_FAILURES} échecs d'affilée"
                        + (f" ({cleared} titre(s) retiré(s) de la file)" if cleared else "")
                        + " : la source est peut-être bloquée."
                    )
                    return None
                continue
            player.started(entry)  # les échecs d'affilée ne sont remis à zéro qu'à la fin normale d'une piste
            self.audio.loaded(player.guild_id, entry.entry_id, entry.track.title, entry.track.source)
            log.info(
                "Musique [serveur %s] étape 2/5 : piste acceptée par Lavalink (entrée %s, source %s, volume %s)",
                player.guild_id, entry.entry_id, entry.track.source, player.volume,
            )
            self._record_history(player.guild_id, entry)
            return entry

    def _record_history(self, guild_id: int, entry: QueueEntry) -> None:
        try:
            self.repo.record_play(guild_id, entry.track, entry.requester_id)
        except Exception:  # l'historique ne doit jamais empêcher la lecture
            log.exception("Historique musical non enregistré (serveur %s)", guild_id)

    def _require_player(self, guild_id: int, *, playing: bool = False) -> GuildPlayer:
        player = self.players.get(guild_id)
        if player is None or not player.connected:
            raise NothingPlaying("Le bot n'est dans aucun salon vocal de ce serveur. Lancez un titre avec /play.")
        if playing and player.current is None:
            raise NothingPlaying("Aucun titre n'est en cours de lecture.")
        return player

    @staticmethod
    def _check_session(player: GuildPlayer, session_id: str | None, entry_id: int | None) -> None:
        if session_id is not None and not player.matches(session_id, entry_id):
            raise StaleControl("Ce panneau ne correspond plus à la lecture en cours : utilisez le panneau le plus récent.")

    async def set_paused(
        self, guild_id: int, listener: Listener, paused: bool, *, session_id: str | None = None, entry_id: int | None = None
    ) -> bool:
        """Met en pause (True) ou reprend (False). Retourne False si c'était déjà le cas."""
        player = self._require_player(guild_id, playing=True)
        async with player.lock:
            self._check_session(player, session_id, entry_id)
            self.policy.require_same_channel(player, listener)
            if player.current is None:
                raise NothingPlaying("Aucun titre n'est en cours de lecture.")
            if (player.state is PlayerState.PAUSED) == paused:
                return False
            await self._call(self.backend.set_paused(guild_id, paused), "pause" if paused else "reprise")
            player.paused() if paused else player.resumed()
        self._notify(guild_id)
        return True

    async def toggle_pause(self, guild_id: int, listener: Listener, *, session_id: str | None = None, entry_id: int | None = None) -> bool:
        """Bouton pause/reprise : décide d'après l'état RÉEL du lecteur. Retourne True si le lecteur est en pause."""
        player = self._require_player(guild_id, playing=True)
        target = player.state is not PlayerState.PAUSED
        await self.set_paused(guild_id, listener, target, session_id=session_id, entry_id=entry_id)
        return target

    async def skip(
        self, guild_id: int, listener: Listener, *, session_id: str | None = None, entry_id: int | None = None
    ) -> tuple[QueueEntry, QueueEntry | None]:
        """Passe le titre en cours. Retourne (titre passé, titre lancé ou None)."""
        player = self._require_player(guild_id, playing=True)
        async with player.lock:
            self._check_session(player, session_id, entry_id)
            current = player.current
            if current is None:
                raise NothingPlaying("Aucun titre n'est en cours de lecture.")
            self.policy.require_skip(player, listener, current)
            player.consecutive_failures = 0
            if player.loop is LoopMode.QUEUE:
                player.queue.append_if_room(self._again(current))  # file en boucle : le titre passé revient à la fin
            if player.queue:
                started = await self._play_next(player)  # remplace la piste en cours (fin « replaced », ignorée)
            else:
                await self._call(self.backend.stop(guild_id), "arrêt")
                player.finished()
                started = None
        self._notify(guild_id)
        return current, started

    async def stop(self, guild_id: int, listener: Listener, *, session_id: str | None = None, entry_id: int | None = None) -> int:
        """Arrête la lecture et vide la file (l'historique et les favoris restent). Retourne le nombre de titres retirés."""
        player = self._require_player(guild_id)
        async with player.lock:
            self._check_session(player, session_id, entry_id)
            self.policy.require_dj(player, listener, "arrêter la musique", admin_anywhere=True)
            cleared = player.queue.clear()
            if player.current is not None:
                await self._call(self.backend.stop(guild_id), "arrêt")
            player.finished()
            player.last_problem = None
        self._notify(guild_id)
        return cleared

    async def leave(self, guild_id: int, listener: Listener | None) -> int:
        """Fait partir le bot du salon vocal. ``listener=None`` : départ automatique (inactivité). Retourne les titres retirés."""
        player = self._require_player(guild_id)
        async with player.lock:
            if listener is not None:
                self.policy.require_dj(player, listener, "faire partir le bot", admin_anywhere=True)
            try:
                await self._call(self.backend.disconnect(guild_id), "départ du salon vocal")
            except MusicError:
                log.warning("Départ du salon vocal incomplet (serveur %s) : le lecteur est réinitialisé quand même", guild_id)
            cleared = player.disconnected()
            self.audio.forget(guild_id)
        self._notify(guild_id)
        return cleared

    async def set_volume(self, guild_id: int, listener: Listener, value: int) -> int:
        if not MIN_VOLUME <= value <= MAX_VOLUME:
            raise InvalidQuery(f"Le volume doit être compris entre {MIN_VOLUME} et {MAX_VOLUME} (valeur reçue : {value}).")
        player = self._require_player(guild_id)
        async with player.lock:  # deux changements simultanés : appliqués l'un après l'autre, le dernier gagne
            self.policy.require_dj(player, listener, "changer le volume")
            await self._call(self.backend.set_volume(guild_id, value), "volume")
            player.volume = value
            try:
                self.repo.set_volume(guild_id, value)
            except Exception:
                log.exception("Volume non enregistré (serveur %s)", guild_id)
        self._notify(guild_id)
        return value

    async def join(self, guild_id: int, listener: Listener, *, text_channel_id: int | None = None) -> bool:
        """Fait venir le bot dans le salon vocal de la personne, sans rien lancer. Retourne False s'il y était déjà."""
        self.require_available()
        player = self._player(guild_id)
        async with player.lock:
            self.policy.require_same_channel(player, listener)  # déjà connecté ailleurs : refus clair
            if player.connected:
                return False
            await self._call(self.backend.connect(guild_id, listener.voice_channel_id), "connexion au salon vocal")
            player.connected_to(listener.voice_channel_id)
            if text_channel_id is not None:
                player.text_channel_id = text_channel_id
        self._notify(guild_id)
        return True

    async def shuffle(self, guild_id: int, listener: Listener) -> int:
        """Mélange la file d'attente (le titre en cours continue). Retourne le nombre de titres mélangés."""
        player = self._require_player(guild_id)
        async with player.lock:
            self.policy.require_dj(player, listener, "mélanger la file")
            if len(player.queue) < 2:
                raise InvalidQuery("Il faut au moins deux titres en attente pour mélanger la file.")
            count = player.queue.shuffle(self._rng)
        self._notify(guild_id)
        return count

    async def remove(self, guild_id: int, listener: Listener, position: int) -> QueueEntry:
        """Retire le titre à la position ``position`` de la file (le sien, ou n'importe lequel pour une personne DJ)."""
        player = self._require_player(guild_id)
        async with player.lock:
            self.policy.require_same_channel(player, listener)
            entry = player.queue.peek_at(position)
            if entry is None:
                raise InvalidQuery(
                    f"Aucun titre en position {position} : la file en compte {len(player.queue)} (voir /queue)."
                    if player.queue else "La file d'attente est vide."
                )
            if entry.requester_id != listener.user_id:
                self.policy.require_dj(player, listener, "retirer le titre de quelqu'un d'autre")
            player.queue.remove_at(position)
        self._notify(guild_id)
        return entry

    async def clear(self, guild_id: int, listener: Listener) -> int:
        """Vide la file d'attente, sans arrêter le titre en cours. Retourne le nombre de titres retirés."""
        player = self._require_player(guild_id)
        async with player.lock:
            self.policy.require_dj(player, listener, "vider la file", admin_anywhere=True)
            cleared = player.queue.clear()
        self._notify(guild_id)
        return cleared

    async def set_loop(self, guild_id: int, listener: Listener, mode: LoopMode | str | None = None) -> LoopMode:
        """Choisit le mode de répétition (``None`` : passe au mode suivant, pour le bouton du panneau)."""
        player = self._require_player(guild_id)
        async with player.lock:
            self.policy.require_dj(player, listener, "changer la répétition")
            if mode is None:
                order = list(LoopMode)
                player.loop = order[(order.index(player.loop) + 1) % len(order)]
            else:
                player.loop = LoopMode(mode)
        self._notify(guild_id)
        return player.loop

    # --- Suivi du son (diagnostic) --------------------------------------------------------

    def on_track_started(self, guild_id: int, entry_id: int | None) -> None:
        """Lavalink a démarré la piste (TrackStartEvent) : étape 3."""
        trace = self.audio.started(guild_id, entry_id)
        if trace is not None and 3 not in trace.logged:
            trace.logged.add(3)
            log.info("Musique [serveur %s] étape 3/5 : Lavalink a démarré la piste (entrée %s)", guild_id, entry_id)

    def on_player_update(self, guild_id: int, *, connected: bool, ping_ms: int, position_ms: int) -> None:
        """État périodique du lecteur (toutes les 5 s) : étapes 4 et 5."""
        player = self.players.get(guild_id)
        paused = player is not None and player.state is PlayerState.PAUSED
        trace = self.audio.player_update(guild_id, connected=connected, ping_ms=ping_ms, position_ms=position_ms, paused=paused)
        if trace is None:
            return
        if connected and 4 not in trace.logged:
            trace.logged.add(4)
            log.info("Musique [serveur %s] étape 4/5 : Lavalink est connecté au serveur vocal (ping %s ms)", guild_id, ping_ms)
        elif not connected and trace.started_at is not None and -4 not in trace.logged:
            trace.logged.add(-4)
            log.warning("Musique [serveur %s] étape 4/5 ÉCHOUÉE : Lavalink n'est PAS connecté au serveur vocal de Discord", guild_id)
        if trace.audio_seen_at is not None and 5 not in trace.logged:
            trace.logged.add(5)
            log.info("Musique [serveur %s] étape 5/5 : la lecture avance, le son part vers Discord", guild_id)

    def on_voice_closed(self, guild_id: int, code: int, reason: str, by_remote: bool) -> str:
        """Discord (ou Lavalink) a fermé la connexion vocale : la cause est journalisée et affichée sur le panneau."""
        text = self.audio.voice_closed(guild_id, code, reason, by_remote)
        log.warning("Musique [serveur %s] : connexion vocale de Lavalink fermée, %s", guild_id, text)
        player = self.players.get(guild_id)
        if player is not None and player.connected and code != 1000:
            player.last_problem = f"Connexion vocale fermée par Discord : {text}."
            self._notify(guild_id)
        return text

    def audio_report(self, guild_id: int) -> AudioReport:
        player = self.players.get(guild_id)
        playing = player is not None and player.current is not None
        paused = player is not None and player.state is PlayerState.PAUSED
        return self.audio.report(guild_id, playing=playing, paused=paused, lavalink=getattr(self.backend, "info", None))

    def check_audio(self, guild_id: int, entry_id: int) -> str | None:
        """Appelé quelques secondes après le lancement de ``entry_id`` : affiche un problème de son sur le panneau."""
        player = self.players.get(guild_id)
        if player is None or player.current is None or player.current.entry_id != entry_id:
            return None
        problem = self.audio_report(guild_id).problem
        trace = self.audio.trace(guild_id)
        if problem is None or trace is None or 0 in trace.logged:
            return None
        trace.logged.add(0)
        log.warning("Musique [serveur %s] : pas de son probable : %s", guild_id, problem)
        player.last_problem = f"Son : {problem}"
        self._notify(guild_id)
        return problem

    def lavalink_problems(self) -> list[str]:
        info = getattr(self.backend, "info", None)
        return info.problems(self.config.sources) if info is not None else []

    @staticmethod
    def loop_label(mode: LoopMode) -> str:
        return LOOP_LABELS[mode]

    # --- Événements du moteur audio ------------------------------------------------------

    async def on_track_end(self, guild_id: int, entry_id: int | None, reason: str) -> QueueEntry | None:
        """Fin d'une piste signalée par Lavalink. Retourne la piste lancée ensuite, s'il y en a une."""
        player = self.players.get(guild_id)
        if player is None:
            return None
        async with player.lock:
            if player.current is None or entry_id != player.current.entry_id:
                return None  # événement d'une piste déjà remplacée ou arrêtée : rien à faire
            if reason == "cleanup":
                player.disconnected()
                started = None
            elif reason in ADVANCE_REASONS:
                if reason == "finished":
                    player.consecutive_failures = 0
                    self._recycle_finished(player, player.current)
                else:
                    player.consecutive_failures += 1
                    player.last_problem = f"« {player.current.track.title} » n'a pas pu être lu et a été passé."
                if player.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    cleared = player.queue.clear()
                    player.finished()
                    player.consecutive_failures = 0
                    player.last_problem = (
                        f"Lecture arrêtée après {MAX_CONSECUTIVE_FAILURES} échecs d'affilée"
                        + (f" ({cleared} titre(s) retiré(s) de la file)" if cleared else "")
                        + " : la source est peut-être bloquée."
                    )
                    started = None
                else:
                    started = await self._play_next_safely(player)
            else:
                return None  # « stopped » / « replaced » : provoqué par le bot lui-même
        self._notify(guild_id)
        return started

    @staticmethod
    def _again(entry: QueueEntry) -> QueueEntry:
        """Nouvelle entrée pour le même titre (nouvel identifiant : un ancien événement ne la confond jamais)."""
        return QueueEntry(entry.track, entry.requester_id, entry.requester_name)

    def _recycle_finished(self, player: GuildPlayer, entry: QueueEntry) -> None:
        """Répétition après une fin NORMALE (jamais après un échec, pour ne pas boucler sur un titre illisible)."""
        if player.loop is LoopMode.TRACK:
            player.queue.push_front(self._again(entry))
        elif player.loop is LoopMode.QUEUE:
            player.queue.append_if_room(self._again(entry))

    async def _play_next_safely(self, player: GuildPlayer) -> QueueEntry | None:
        try:
            return await self._play_next(player)
        except Exception:
            log.exception("Passage au titre suivant impossible (serveur %s)", player.guild_id)
            player.finished()
            return None

    async def on_track_problem(self, guild_id: int, entry_id: int | None, message: str, *, stuck: bool) -> QueueEntry | None:
        """Exception ou blocage d'une piste. Une piste bloquée est passée ; après une exception, Lavalink
        envoie lui-même une fin « loadFailed » (traitée par ``on_track_end``)."""
        player = self.players.get(guild_id)
        if player is None:
            return None
        log.warning("Lecture musicale (serveur %s) : %s", guild_id, message)
        self.audio.track_failed(guild_id, entry_id, message, stuck=stuck)
        if not stuck:
            return None
        async with player.lock:
            if player.current is None or entry_id != player.current.entry_id:
                return None
            player.last_problem = f"« {player.current.track.title} » était bloqué et a été passé."
            player.consecutive_failures += 1
            if player.consecutive_failures >= MAX_CONSECUTIVE_FAILURES or not player.queue:
                try:
                    await self._call(self.backend.stop(guild_id), "arrêt")
                except MusicError:
                    pass
                if player.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    player.queue.clear()
                    player.consecutive_failures = 0
                player.finished()
                started = None
            else:
                started = await self._play_next_safely(player)
        self._notify(guild_id)
        return started

    async def on_voice_moved(self, guild_id: int, channel_id: int | None) -> None:
        """Le bot a été déplacé (``channel_id``) ou sorti (None) du salon vocal par quelqu'un d'autre."""
        player = self.players.get(guild_id)
        if player is None or not player.connected:
            return
        async with player.lock:
            if channel_id is None:
                player.disconnected()
                self.audio.forget(guild_id)
            else:
                player.voice_channel_id = channel_id
        self._notify(guild_id)

    async def reset_all(self, reason: str) -> int:
        """Le moteur audio a été perdu : tous les lecteurs sont remis à zéro. Retourne le nombre de lecteurs concernés."""
        count = 0
        for player in self.players.all():
            if not player.connected:
                continue
            async with player.lock:
                player.disconnected()
                player.last_problem = reason
            self.audio.forget(player.guild_id)
            count += 1
            self._notify(player.guild_id)
        return count

    async def shutdown(self) -> None:
        """Arrêt du bot : le bot quitte proprement chaque salon vocal."""
        for player in self.players.all():
            if not player.connected:
                continue
            try:
                await asyncio.wait_for(self.backend.disconnect(player.guild_id), 3)
            except Exception:
                log.warning("Départ du salon vocal impossible à l'arrêt (serveur %s)", player.guild_id)
            player.disconnected()

    # --- Historique et favoris ----------------------------------------------------------------

    def history(self, guild_id: int, *, limit: int, offset: int = 0) -> tuple[list[HistoryRow], int]:
        return self.repo.history(guild_id, limit=limit, offset=offset), self.repo.history_count(guild_id)

    def favorites(self, guild_id: int, user_id: int) -> list[FavoriteRow]:
        return self.repo.favorites(guild_id, user_id)

    def toggle_favorite(
        self, guild_id: int, user_id: int, *, session_id: str | None = None, entry_id: int | None = None
    ) -> tuple[Track, bool]:
        """Ajoute le titre EN COURS aux favoris de la personne, ou l'en retire. Retourne (titre, ajouté ?)."""
        player = self._require_player(guild_id, playing=True)
        self._check_session(player, session_id, entry_id)
        track = player.current.track
        if not track.uri:
            raise InvalidQuery("Ce titre n'a pas de lien public : il ne pourrait pas être rejoué, il n'est donc pas mis en favori.")
        if self.repo.is_favorite(guild_id, user_id, track.key):
            self.repo.remove_favorite(guild_id, user_id, track.key)
            return track, False
        try:
            added = self.repo.add_favorite(guild_id, user_id, track)
        except FavoritesFull:
            raise InvalidQuery("Vous avez atteint le nombre maximal de favoris : retirez-en avec /favorite remove.") from None
        return track, added

    def add_favorite(self, guild_id: int, user_id: int) -> tuple[Track, bool]:
        """Ajoute le titre EN COURS aux favoris (/favorite add). Retourne (titre, ajouté ?) : False s'il y était déjà."""
        player = self._require_player(guild_id, playing=True)
        track = player.current.track
        if not track.uri:
            raise InvalidQuery("Ce titre n'a pas de lien public : il ne pourrait pas être rejoué, il n'est donc pas mis en favori.")
        if self.repo.is_favorite(guild_id, user_id, track.key):
            return track, False
        try:
            return track, self.repo.add_favorite(guild_id, user_id, track)
        except FavoritesFull:
            raise InvalidQuery("Vous avez atteint le nombre maximal de favoris : retirez-en avec /favorite remove.") from None

    def remove_favorite_at(self, guild_id: int, user_id: int, number: int) -> FavoriteRow:
        favorites = self.repo.favorites(guild_id, user_id)
        if not 1 <= number <= len(favorites):
            raise InvalidQuery(f"Aucun favori n° {number} (vous en avez {len(favorites)} ; voir /favorite list).")
        favorite = favorites[number - 1]
        self.repo.remove_favorite(guild_id, user_id, favorite.track_key)
        return favorite


def source_choices(enabled: Sequence[str]) -> list[tuple[str, str]]:
    """(libellé, clé) des sources activées, pour le menu de /play et /search."""
    return [(SOURCES[key].label, key) for key in enabled]
