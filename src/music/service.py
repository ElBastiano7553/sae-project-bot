"""Orchestration de la musique : recherche, file, lecture, volume, historique et favoris.

Le service ne connaît ni Discord ni wavelink : il reçoit des ``Listener`` (la personne qui
agit, décrite par le cog) et parle au moteur audio à travers ``AudioBackend``. Les tests
utilisent un faux moteur ; en production, c'est ``lavalink.WavelinkBackend``.

Concurrence : toute opération qui modifie un lecteur prend ``GuildPlayer.lock``. Une fin de
piste, un /musique passer et une erreur de lecture arrivés en même temps sont donc traités
l'un après l'autre, et chaque événement de fin porte l'identifiant de l'entrée concernée :
un événement qui ne correspond plus au titre en cours est ignoré (jamais deux « suivant »).

Après chaque changement visible, ``on_change(guild_id)`` est appelé (le cog actualise le panneau).
"""

from __future__ import annotations

import asyncio
import logging
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
from src.music.models import QueueEntry, SearchOutcome, Track
from src.music.permissions import Listener, MusicPolicy
from src.music.player import GuildPlayer, PlayerManager, PlayerState
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


class AudioBackend(Protocol):
    """Ce que le service attend du moteur audio (Lavalink en production)."""

    @property
    def ready(self) -> bool: ...

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
            raise NothingPlaying("Le bot n'est dans aucun salon vocal de ce serveur. Lancez un titre avec /musique jouer.")
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
            raise InvalidQuery("Vous avez atteint le nombre maximal de favoris : retirez-en avec /musique favori retirer:<numéro>.") from None
        return track, added

    def remove_favorite_at(self, guild_id: int, user_id: int, number: int) -> FavoriteRow:
        favorites = self.repo.favorites(guild_id, user_id)
        if not 1 <= number <= len(favorites):
            raise InvalidQuery(f"Aucun favori n° {number} (vous en avez {len(favorites)} ; voir /musique favoris).")
        favorite = favorites[number - 1]
        self.repo.remove_favorite(guild_id, user_id, favorite.track_key)
        return favorite


def source_choices(enabled: Sequence[str]) -> list[tuple[str, str]]:
    """(libellé, clé) des sources activées, pour le menu de /musique jouer."""
    return [(SOURCES[key].label, key) for key in enabled]
