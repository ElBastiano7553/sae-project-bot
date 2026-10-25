"""Adaptateur entre le service de musique et Lavalink v4, par la bibliothèque wavelink.

C'est le seul module qui importe wavelink, et il ne le fait qu'à l'activation de la musique
(MUSIC_ENABLED=true) : sans musique, wavelink n'est jamais chargé.

* Connexion : ``start()`` lance une tâche de fond (wavelink réessaie indéfiniment tant que
  Lavalink est absent, ce qui ne doit jamais bloquer le démarrage du bot). ``ready`` dit si
  Lavalink est joignable à l'instant.
* Pistes : les pistes renvoyées par Lavalink restent au format brut de Lavalink v4 ; c'est
  ``search.normalize_track`` qui les nettoie. À la lecture, l'identifiant de l'entrée de la file
  est joint à la piste (« userData ») : Lavalink le renvoie dans les événements de fin.
* Le son ne passe jamais par le bot : Lavalink le reçoit des sources, l'encode et l'envoie
  à Discord (chiffrement de bout en bout DAVE compris, Lavalink ≥ 4.2.0). PyNaCl n'est pas nécessaire.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from src.music.diagnostics import LavalinkInfo
from src.music.errors import BackendError
from src.music.models import Track
from src.music.service import LiveVoiceState, LoadResult

log = logging.getLogger(__name__)

ENTRY_KEY = "samae_entry"
CONNECT_TIMEOUT_SECONDS = 10.0
NODE_IDENTIFIER = "samae"
# Vérification de la connexion à Lavalink. wavelink 3.5.2 ne remarque pas toujours l'arrêt de Lavalink
# (sa tâche de lecture du websocket s'arrête sur « Server disconnected » sans changer l'état du nœud,
# constaté avec Lavalink 4.2.2) : sans cette surveillance, la musique resterait cassée jusqu'au
# redémarrage du bot après un simple redémarrage de Lavalink.
WATCHDOG_SECONDS = 10.0


def entry_id_of(track: Any) -> int | None:
    """Identifiant de l'entrée de la file joint à une piste wavelink (``None`` s'il manque ou est invalide)."""
    try:
        value = dict(track.extras).get(ENTRY_KEY)
    except Exception:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class WavelinkBackend:
    def __init__(self, bot: discord.Client, *, uri: str, password: str, idle_timeout_seconds: int) -> None:
        import wavelink  # import ici : wavelink n'est chargé que si la musique est activée

        self._wavelink = wavelink
        self._bot = bot
        self._uri = uri
        self._password = password
        self._idle_timeout = idle_timeout_seconds
        self._node: Any = None
        self._task: asyncio.Task | None = None
        self.watchdog_seconds = WATCHDOG_SECONDS
        self.info: LavalinkInfo | None = None  # version et sources annoncées par Lavalink (après connexion)

    # --- Connexion à Lavalink ---------------------------------------------------------

    def start(self) -> asyncio.Task:
        """Lance la connexion en tâche de fond (supervisée : une erreur est journalisée, jamais silencieuse)."""
        self._node = self._wavelink.Node(
            identifier=NODE_IDENTIFIER,
            uri=self._uri,
            password=self._password,
            client=self._bot,
            inactive_player_timeout=self._idle_timeout,
        )
        self._task = asyncio.create_task(self._connect(), name="lavalink-connect")
        self._task.add_done_callback(self._on_connect_done)
        return self._task

    async def _connect(self) -> None:
        await self._wavelink.Pool.connect(nodes=[self._node], client=self._bot)
        if NODE_IDENTIFIER in self._wavelink.Pool.nodes:
            await self._watch()

    def _socket_lost(self) -> bool:
        node = self._node
        if node.status is not self._wavelink.NodeStatus.CONNECTED:
            return False  # déconnexion déjà connue de wavelink, qui se reconnecte lui-même
        websocket = getattr(node, "_websocket", None)
        reader = getattr(websocket, "keep_alive_task", None)
        return websocket is None or reader is None or reader.done()

    async def _watch(self) -> None:
        """Surveille la connexion ; si elle est perdue sans que wavelink le voie, la ferme et se reconnecte."""
        while True:
            await asyncio.sleep(self.watchdog_seconds)
            if not self._socket_lost():
                continue
            log.warning("Connexion à Lavalink perdue (Lavalink arrêté ou redémarré) : nouvelle connexion en cours")
            websocket = getattr(self._node, "_websocket", None)
            if websocket is not None:
                await websocket.cleanup()  # état « déconnecté » et événement node_disconnected (lecteurs réinitialisés)
            else:
                self._node._status = self._wavelink.NodeStatus.DISCONNECTED
            await self._leave_orphan_channels()
            await self._wavelink.Pool.reconnect()  # réessaie indéfiniment, avec un délai croissant
            if self.ready:
                log.info("Connexion à Lavalink rétablie")

    def _on_connect_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            log.error("Connexion à Lavalink impossible : la musique reste indisponible", exc_info=task.exception())
        elif NODE_IDENTIFIER not in self._wavelink.Pool.nodes:
            # wavelink n'ajoute le nœud au « Pool » qu'après une connexion acceptée (le message « ready » de
            # Lavalink peut arriver juste après : ne pas se fier à ``ready`` à cet instant).
            log.error(
                "Lavalink a refusé la connexion (mot de passe LAVALINK_PASSWORD incorrect ou Lavalink qui n'est pas en v4) : "
                "la musique reste indisponible jusqu'au prochain redémarrage du bot."
            )

    async def _leave_orphan_channels(self) -> None:
        """Les lecteurs de l'ancienne session Lavalink sont perdus : le bot quitte les salons vocaux concernés."""
        for voice in list(getattr(self._bot, "voice_clients", ())):
            if isinstance(voice, self._wavelink.Player):
                try:
                    await voice.disconnect()
                except Exception:
                    log.debug("Départ d'un salon vocal après la perte de Lavalink incomplet", exc_info=True)

    async def refresh_info(self) -> LavalinkInfo | None:
        """Lit la version et les sources de Lavalink (``GET /v4/info``) : un Lavalink sans DAVE ne peut pas émettre de son."""
        if not self.ready:
            return None
        try:
            raw = await asyncio.wait_for(self._node.fetch_info(), CONNECT_TIMEOUT_SECONDS)
        except Exception as exc:  # information utile mais pas indispensable
            log.warning("Version de Lavalink illisible : %s", type(exc).__name__)
            return self.info
        self.info = LavalinkInfo(
            version=str(raw.version.semver),
            source_managers=tuple(str(name) for name in raw.source_managers),
            plugins=tuple(f"{plugin.name} {plugin.version}" for plugin in raw.plugins),
        )
        return self.info

    @property
    def ready(self) -> bool:
        node = self._node
        return node is not None and node.status is self._wavelink.NodeStatus.CONNECTED

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._node is not None:
            try:
                await self._node.close(eject=True)
            except Exception:
                log.debug("Fermeture du nœud Lavalink incomplète", exc_info=True)
            session = getattr(self._node, "_session", None)
            if session is not None and not session.closed:
                await session.close()

    # --- Recherche --------------------------------------------------------------------

    async def load(self, identifier: str) -> LoadResult:
        wavelink = self._wavelink
        if not self.ready:
            raise BackendError("Lavalink n'est pas connecté")
        try:
            result = await wavelink.Pool.fetch_tracks(identifier, node=self._node)
        except wavelink.LavalinkLoadException as exc:
            raise BackendError(f"chargement refusé par Lavalink ({getattr(exc, 'severity', '?')}) : {getattr(exc, 'error', exc)}") from None
        except (wavelink.LavalinkException, wavelink.NodeException) as exc:
            raise BackendError(f"Lavalink a répondu par une erreur : {type(exc).__name__}") from None
        if isinstance(result, wavelink.Playlist):
            return LoadResult([dict(track.raw_data) for track in result.tracks], playlist_name=result.name or "Playlist")
        return LoadResult([dict(track.raw_data) for track in result])

    # --- Lecteurs ---------------------------------------------------------------------

    def _player(self, guild_id: int):
        guild = self._bot.get_guild(guild_id)
        voice = guild.voice_client if guild is not None else None
        return voice if isinstance(voice, self._wavelink.Player) else None

    def _require_player(self, guild_id: int):
        player = self._player(guild_id)
        if player is None:
            raise BackendError("aucun lecteur Lavalink pour ce serveur")
        return player

    async def connect(self, guild_id: int, channel_id: int) -> None:
        guild = self._bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise BackendError("salon vocal introuvable")
        current = self._player(guild_id)
        try:
            if current is not None and current.connected:
                if current.channel is None or current.channel.id != channel_id:
                    await current.move_to(channel)
                return
            player = await channel.connect(cls=self._wavelink.Player, self_deaf=True, timeout=CONNECT_TIMEOUT_SECONDS)
        except (self._wavelink.ChannelTimeoutException, asyncio.TimeoutError, TimeoutError):
            raise BackendError("connexion au salon vocal trop longue") from None
        except discord.ClientException as exc:
            raise BackendError(f"connexion au salon vocal refusée : {exc}") from None
        player.autoplay = self._wavelink.AutoPlayMode.disabled  # la file est gérée par Samae
        # Lavalink 4.2 exige l'identifiant du salon pour DAVE ; wavelink 3.5 le transmet avec la session vocale.
        sent_channel = bool(getattr(player, "_voice_state", {}).get("channel_id"))
        log.info("Musique [serveur %s] : salon vocal rejoint, session vocale transmise à Lavalink (salon inclus : %s)",
                 guild_id, "oui" if sent_channel else "NON")

    async def play(self, guild_id: int, track: Track, *, entry_id: int, volume: int) -> None:
        wavelink = self._wavelink
        player = self._require_player(guild_id)
        data = dict(track.payload)
        data["userData"] = {ENTRY_KEY: entry_id}
        playable = wavelink.Playable(data=data)  # type: ignore[arg-type]
        try:
            await player.play(playable, replace=True, volume=max(volume, 1), paused=False, add_history=False)
            if volume == 0:  # wavelink confond 0 avec « volume inchangé »
                await player.set_volume(0)
        except wavelink.LavalinkException as exc:
            raise BackendError(f"lecture refusée par Lavalink (HTTP {getattr(exc, 'status', '?')})") from None

    async def set_paused(self, guild_id: int, paused: bool) -> None:
        try:
            await self._require_player(guild_id).pause(paused)
        except self._wavelink.LavalinkException as exc:
            raise BackendError(f"pause refusée par Lavalink (HTTP {getattr(exc, 'status', '?')})") from None

    async def stop(self, guild_id: int) -> None:
        try:
            await self._require_player(guild_id).skip(force=True)
        except self._wavelink.LavalinkException as exc:
            raise BackendError(f"arrêt refusé par Lavalink (HTTP {getattr(exc, 'status', '?')})") from None

    async def set_volume(self, guild_id: int, volume: int) -> None:
        try:
            await self._require_player(guild_id).set_volume(volume)
        except self._wavelink.LavalinkException as exc:
            raise BackendError(f"volume refusé par Lavalink (HTTP {getattr(exc, 'status', '?')})") from None

    async def disconnect(self, guild_id: int) -> None:
        player = self._player(guild_id)
        if player is None:
            return
        await player.disconnect()

    async def live_voice_state(self, guild_id: int) -> LiveVoiceState | None:
        """État du lecteur vu par Lavalink à l'instant (``GET …/players/<serveur>``), sans aucune donnée secrète."""
        if not self.ready:
            return None
        try:
            info = await asyncio.wait_for(self._node.fetch_player_info(guild_id), CONNECT_TIMEOUT_SECONDS)
        except Exception as exc:
            log.warning("État du lecteur Lavalink illisible (serveur %s) : %s", guild_id, type(exc).__name__)
            return None
        if info is None:
            return LiveVoiceState(exists=False)
        voice = info.voice_state
        return LiveVoiceState(
            exists=True,
            connected=bool(info.state.connected),
            ping_ms=int(info.state.ping),
            position_ms=int(info.state.position),
            paused=bool(info.paused),
            has_track=info.track is not None,
            volume=int(info.volume),
            voice_sent=bool(voice.session_id and voice.endpoint and voice.token),
            channel_sent=bool(voice.channel_id),
        )

    def position_ms(self, guild_id: int) -> int | None:
        player = self._player(guild_id)
        return int(player.position) if player is not None and player.current is not None else None
