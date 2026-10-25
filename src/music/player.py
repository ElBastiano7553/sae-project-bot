"""Lecteur d'UN serveur Discord (état, file, volume) et gestionnaire des lecteurs.

États et transitions autorisées (toute autre transition lève ``InvalidTransition``) :

    DISCONNECTED ──connecté──▶ IDLE ──lecture──▶ PLAYING ◀──reprise── PAUSED
         ▲                      ▲                  │ └────pause──────▶  │
         │                      └──file vide / arrêt┴───────────────────┘
         └────────── départ du salon, déconnexion, Lavalink perdu (depuis n'importe quel état)

* ``PLAYING`` → ``PLAYING`` : passage à la piste suivante (fin, /skip, erreur de lecture).
* Une **session** commence à chaque connexion au salon vocal : son identifiant aléatoire est
  inscrit dans les boutons du panneau, si bien qu'un bouton d'une session terminée (ou d'avant
  un redémarrage du bot) ne commande jamais la session suivante.
* Chaque serveur a son propre lecteur, sa file et son verrou : rien n'est partagé entre serveurs.

Ce module ne fait aucune entrée-sortie : le service (``service.MusicService``) appelle le moteur
audio puis enregistre ici le résultat, toujours sous ``GuildPlayer.lock``.
"""

from __future__ import annotations

import asyncio
import secrets
from enum import StrEnum

from src.music.models import QueueEntry
from src.music.queue import TrackQueue


class PlayerState(StrEnum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


TRANSITIONS: dict[PlayerState, frozenset[PlayerState]] = {
    PlayerState.DISCONNECTED: frozenset({PlayerState.IDLE}),
    PlayerState.IDLE: frozenset({PlayerState.PLAYING, PlayerState.DISCONNECTED}),
    PlayerState.PLAYING: frozenset({PlayerState.PLAYING, PlayerState.PAUSED, PlayerState.IDLE, PlayerState.DISCONNECTED}),
    PlayerState.PAUSED: frozenset({PlayerState.PLAYING, PlayerState.IDLE, PlayerState.DISCONNECTED}),
}


class LoopMode(StrEnum):
    """Répétition : aucune, le titre en cours, ou toute la file (le titre fini repart en fin de file)."""

    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


LOOP_LABELS = {LoopMode.OFF: "désactivée", LoopMode.TRACK: "titre en cours", LoopMode.QUEUE: "toute la file"}


class InvalidTransition(RuntimeError):
    """Erreur de programmation : le service a tenté une transition interdite."""


class GuildPlayer:
    def __init__(self, guild_id: int, *, max_queue: int, volume: int) -> None:
        self.guild_id = guild_id
        self.state = PlayerState.DISCONNECTED
        self.session_id = ""
        self.voice_channel_id: int | None = None
        self.text_channel_id: int | None = None  # salon du panneau
        self.panel_message_id: int | None = None
        self.current: QueueEntry | None = None
        self.queue = TrackQueue(max_queue)
        self.volume = volume
        self.consecutive_failures = 0
        self.last_problem: str | None = None  # dernier problème de lecture, affiché sur le panneau
        self.loop = LoopMode.OFF
        # Toutes les opérations qui changent ce lecteur passent par ce verrou (commandes, boutons,
        # événements de Lavalink) : une seule transition à la fois, jamais deux « piste suivante ».
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.state is not PlayerState.DISCONNECTED

    @property
    def active(self) -> bool:
        return self.state in (PlayerState.PLAYING, PlayerState.PAUSED)

    def _move(self, target: PlayerState) -> None:
        if target not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state} → {target}")
        self.state = target

    # --- Transitions ------------------------------------------------------------

    def connected_to(self, voice_channel_id: int) -> str:
        """Début d'une session dans ``voice_channel_id``. Retourne l'identifiant de la nouvelle session."""
        self._move(PlayerState.IDLE)
        self.voice_channel_id = voice_channel_id
        self.session_id = secrets.token_hex(4)
        self.panel_message_id = None
        self.consecutive_failures = 0
        self.last_problem = None
        self.loop = LoopMode.OFF
        return self.session_id

    def started(self, entry: QueueEntry) -> None:
        """Nouvelle piste lancée (depuis IDLE, PLAYING ou PAUSED : une nouvelle piste démarre toujours sans pause)."""
        self._move(PlayerState.PLAYING)
        self.current = entry

    def paused(self) -> None:
        self._move(PlayerState.PAUSED)

    def resumed(self) -> None:
        if self.state is not PlayerState.PAUSED:
            raise InvalidTransition(f"{self.state} → playing (reprise sans pause)")
        self._move(PlayerState.PLAYING)

    def finished(self) -> None:
        """Plus rien à jouer (file vide ou arrêt) : le bot reste dans le salon."""
        if self.active:
            self._move(PlayerState.IDLE)
        self.current = None

    def disconnected(self) -> int:
        """Fin de la session : file vidée (l'historique et les favoris sont en base, pas ici)."""
        cleared = self.queue.clear()
        if self.state is not PlayerState.DISCONNECTED:
            self._move(PlayerState.DISCONNECTED)
        self.current = None
        self.voice_channel_id = None
        self.session_id = ""
        return cleared

    def matches(self, session_id: str, entry_id: int | None = None) -> bool:
        """Vrai si un bouton (session, piste affichée) correspond encore à ce lecteur."""
        if not self.connected or not session_id or session_id != self.session_id:
            return False
        if entry_id is None:
            return True
        current_id = self.current.entry_id if self.current else 0
        return entry_id == current_id


class PlayerManager:
    """Un lecteur par serveur, créé à la demande. Les identifiants de serveur ne se mélangent jamais."""

    def __init__(self, *, max_queue: int, default_volume: int) -> None:
        self._players: dict[int, GuildPlayer] = {}
        self._max_queue = max_queue
        self._default_volume = default_volume

    def get(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    def get_or_create(self, guild_id: int, *, volume: int | None = None) -> GuildPlayer:
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(guild_id, max_queue=self._max_queue, volume=self._default_volume if volume is None else volume)
            self._players[guild_id] = player
        return player

    def all(self) -> list[GuildPlayer]:
        return list(self._players.values())

    def __len__(self) -> int:
        return len(self._players)
