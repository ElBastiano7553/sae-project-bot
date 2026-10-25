"""Suivi du chemin du son, étape par étape, pour savoir POURQUOI rien ne s'entend.

Une recherche réussie ne prouve pas que le son part : Lavalink peut accepter une piste sans
jamais réussir à se connecter au serveur vocal de Discord (chiffrement DAVE refusé, réseau UDP
bloqué, Lavalink trop ancien…). Ce module range ce que Lavalink rapporte, sans aucune
dépendance à Discord ni à wavelink, en cinq étapes distinctes :

1. **recherche** : la source a renvoyé au moins un titre lisible ;
2. **piste chargée** : Lavalink a accepté la piste (requête de lecture sans erreur) ;
3. **lecteur démarré** : Lavalink a envoyé « TrackStartEvent » pour CETTE piste ;
4. **connexion vocale** : Lavalink se dit connecté au serveur vocal (``playerUpdate.connected``) ;
5. **audio envoyé** : la position de lecture avance pendant que la connexion vocale est ouverte.
   Lavalink n'avance la position que lorsque la connexion vocale consomme les trames audio :
   c'est la meilleure preuve disponible côté bot que du son part vers Discord.

Ce que le bot ne peut PAS savoir : si les personnes du salon entendent vraiment le son (volume
local du bot à 0 dans Discord, application Discord trop ancienne…). L'étape 5 réussie signifie
« le son quitte Lavalink chiffré pour Discord », pas « le son est audible ».

Aucune donnée secrète n'est conservée ici (ni jeton vocal, ni identifiant de session vocale).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

# Version minimale de Lavalink capable de chiffrer le son avec DAVE (obligatoire sur Discord).
DAVE_MIN_VERSION = (4, 2, 0)
# Délai après le lancement d'une piste au-delà duquel une étape manquante est un problème.
START_GRACE_SECONDS = 8.0
# Lavalink envoie « playerUpdate » toutes les 5 s (playerUpdateInterval) : il faut en recevoir
# deux pour voir la position avancer.
AUDIO_GRACE_SECONDS = 15.0

# Codes de fermeture de la connexion vocale (Discord) : explication pour un humain.
VOICE_CLOSE_CODES: dict[int, str] = {
    4006: "session vocale invalide : Discord a refusé la connexion (relancer /play suffit en général)",
    4009: "délai dépassé avec le serveur vocal de Discord (réseau lent ou bloqué)",
    4011: "serveur vocal introuvable",
    4012: "protocole vocal inconnu de Discord",
    4014: "le bot a été déconnecté du salon (expulsé, déplacé ou salon supprimé)",
    4015: "le serveur vocal de Discord a planté : relancez la lecture",
    4016: "mode de chiffrement refusé par Discord : Lavalink est trop ancien, installez Lavalink 4.2.2",
    4017: "Discord exige le chiffrement DAVE et Lavalink ne l'a pas fourni : installez Lavalink 4.2.0 ou plus récent",
    4020: "requête vocale invalide",
    4021: "trop de connexions vocales en peu de temps (limite de Discord) : patientez une minute",
    4022: "appel terminé par Discord (salon supprimé ou bot retiré)",
}


def explain_close_code(code: int, reason: str = "") -> str:
    text = VOICE_CLOSE_CODES.get(code)
    if text is None:
        text = "fermeture normale" if code == 1000 else "fermeture inattendue"
    detail = f" ({reason[:120]})" if reason else ""
    return f"code {code} : {text}{detail}"


def parse_version(raw: str | None) -> tuple[int, int, int] | None:
    """« 4.2.2 » ou « 4.2.2-rc1 » → (4, 2, 2) ; ``None`` si la version est illisible."""
    if not raw:
        return None
    core = raw.strip().lstrip("v").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:3]):
        return None
    numbers = [int(part) for part in parts[:3]] + [0] * (3 - len(parts[:3]))
    return numbers[0], numbers[1], numbers[2]


@dataclass(frozen=True)
class LavalinkInfo:
    """Ce que Lavalink annonce de lui-même (``GET /v4/info``) : aucune donnée secrète."""

    version: str
    source_managers: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()

    @property
    def supports_dave(self) -> bool | None:
        parsed = parse_version(self.version)
        return None if parsed is None else parsed >= DAVE_MIN_VERSION

    def problems(self, wanted_sources: tuple[str, ...]) -> list[str]:
        """Incompatibilités qui empêchent le son ou une source configurée."""
        problems = []
        if self.supports_dave is False:
            problems.append(
                f"Lavalink {self.version} ne gère pas le chiffrement DAVE exigé par Discord : le bot rejoint le salon "
                "mais aucun son ne peut partir. Installez Lavalink 4.2.2 (docs/music.md)."
            )
        managers = {name.casefold() for name in self.source_managers}
        if managers:
            if any(source in ("youtube", "youtubemusic") for source in wanted_sources) and "youtube" not in managers:
                problems.append(
                    "MUSIC_SOURCES contient YouTube, mais Lavalink n'a pas de source YouTube : le plugin youtube-plugin "
                    "1.18.2 n'est pas chargé (dossier plugins/ de Lavalink)."
                )
            for source in ("soundcloud", "bandcamp"):
                if source in wanted_sources and source not in managers:
                    problems.append(f"MUSIC_SOURCES contient {source}, mais cette source est désactivée dans application.yml de Lavalink.")
        return problems


class StepStatus(StrEnum):
    OK = "ok"
    WAITING = "waiting"  # pas encore observé, encore dans les délais
    FAILED = "failed"
    UNKNOWN = "unknown"  # rien à vérifier (aucune lecture en cours)


STEP_NAMES = ("Recherche", "Piste chargée", "Lecteur démarré", "Connexion vocale", "Audio envoyé")


@dataclass(frozen=True)
class Step:
    name: str
    status: StepStatus
    detail: str


@dataclass(frozen=True)
class AudioReport:
    steps: tuple[Step, ...]
    problem: str | None  # première cause probable, en clair (None : rien d'anormal observé)

    @property
    def all_ok(self) -> bool:
        return all(step.status is StepStatus.OK for step in self.steps)


@dataclass
class AudioTrace:
    """Ce qui a été observé pour la piste en cours d'UN serveur (remis à zéro à chaque piste)."""

    entry_id: int = 0
    title: str = ""
    source: str = ""
    loaded_at: float | None = None
    started_at: float | None = None
    voice_connected: bool | None = None
    ping_ms: int | None = None
    first_position: int | None = None
    last_position: int | None = None
    audio_seen_at: float | None = None
    track_error: str | None = None
    stuck: bool = False
    closed: str | None = None  # dernière fermeture de la connexion vocale, expliquée
    logged: set[int] = field(default_factory=set)  # étapes déjà journalisées pour cette piste


class AudioMonitor:
    """Traces de tous les serveurs (une par serveur, jamais partagée)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._traces: dict[int, AudioTrace] = {}
        self._clock = clock
        self.searches_ok: dict[int, str] = {}  # serveur → source de la dernière recherche réussie
        self.frames: tuple[int, int, int] | None = None  # trames envoyées, nulles, manquantes (Lavalink, par minute)

    def trace(self, guild_id: int) -> AudioTrace | None:
        return self._traces.get(guild_id)

    def forget(self, guild_id: int) -> None:
        self._traces.pop(guild_id, None)

    def search_ok(self, guild_id: int, source: str) -> None:
        self.searches_ok[guild_id] = source

    def loaded(self, guild_id: int, entry_id: int, title: str, source: str) -> AudioTrace:
        """Lavalink a accepté la piste ``entry_id`` : nouvelle trace (la connexion vocale connue est gardée)."""
        previous = self._traces.get(guild_id)
        trace = AudioTrace(entry_id=entry_id, title=title, source=source, loaded_at=self._clock())
        if previous is not None:
            trace.voice_connected, trace.ping_ms, trace.closed = previous.voice_connected, previous.ping_ms, previous.closed
        self._traces[guild_id] = trace
        return trace

    def _current(self, guild_id: int, entry_id: int | None) -> AudioTrace | None:
        trace = self._traces.get(guild_id)
        if trace is None or (entry_id is not None and entry_id != trace.entry_id):
            return None
        return trace

    def started(self, guild_id: int, entry_id: int | None) -> AudioTrace | None:
        trace = self._current(guild_id, entry_id)
        if trace is not None and trace.started_at is None:
            trace.started_at = self._clock()
        return trace

    def player_update(self, guild_id: int, *, connected: bool, ping_ms: int, position_ms: int, paused: bool) -> AudioTrace | None:
        trace = self._traces.get(guild_id)
        if trace is None:
            return None
        trace.voice_connected = connected
        trace.ping_ms = ping_ms if ping_ms >= 0 else None
        if connected:
            trace.closed = None
        if trace.started_at is None or paused:
            return trace
        if trace.first_position is None:
            trace.first_position = position_ms
        elif connected and position_ms > trace.first_position and trace.audio_seen_at is None:
            trace.audio_seen_at = self._clock()
        trace.last_position = position_ms
        return trace

    def voice_closed(self, guild_id: int, code: int, reason: str, by_remote: bool) -> str:
        text = explain_close_code(code, reason) + ("" if by_remote else " (fermée par Lavalink)")
        trace = self._traces.get(guild_id)
        if trace is None:
            trace = self._traces[guild_id] = AudioTrace()
        trace.voice_connected = False
        trace.closed = text
        return text

    def track_failed(self, guild_id: int, entry_id: int | None, message: str, *, stuck: bool = False) -> None:
        trace = self._current(guild_id, entry_id)
        if trace is not None:
            trace.track_error = message[:300]
            trace.stuck = trace.stuck or stuck

    def report(self, guild_id: int, *, playing: bool, paused: bool = False, lavalink: LavalinkInfo | None = None) -> AudioReport:
        """État des cinq étapes pour la piste en cours, et la cause probable d'un silence."""
        now = self._clock()
        trace = self._traces.get(guild_id)
        searched = self.searches_ok.get(guild_id)
        steps: list[Step] = [
            Step(STEP_NAMES[0], StepStatus.OK if searched else StepStatus.UNKNOWN,
                 f"dernière recherche réussie : {searched}" if searched else "aucune recherche depuis le démarrage"),
        ]
        if not playing or trace is None or not trace.loaded_at:
            rest = "aucun titre en cours" if not playing else "en attente de Lavalink"
            steps += [Step(name, StepStatus.UNKNOWN, rest) for name in STEP_NAMES[1:]]
            problem = trace.closed if trace is not None and trace.closed else None
            return AudioReport(tuple(steps), f"Connexion vocale fermée : {problem}." if problem else None)
        age = now - trace.loaded_at
        steps.append(Step(STEP_NAMES[1], StepStatus.OK, f"« {trace.title[:80]} » acceptée par Lavalink"))
        problem: str | None = None

        # 3. Lecteur démarré
        if trace.started_at is not None:
            steps.append(Step(STEP_NAMES[2], StepStatus.OK, "Lavalink a démarré la piste"))
        elif trace.track_error:
            steps.append(Step(STEP_NAMES[2], StepStatus.FAILED, f"la source a refusé la piste : {trace.track_error}"))
            problem = f"La source refuse de fournir le son ({trace.track_error}). Essayez une autre source (option source:)."
        elif age > START_GRACE_SECONDS:
            steps.append(Step(STEP_NAMES[2], StepStatus.FAILED, f"aucun démarrage signalé après {int(age)} s"))
            problem = "Lavalink n'a jamais démarré la piste : regardez les journaux de Lavalink (erreur de la source ?)."
        else:
            steps.append(Step(STEP_NAMES[2], StepStatus.WAITING, "en attente du démarrage"))

        # 4. Connexion vocale
        if trace.voice_connected:
            ping = f" (ping {trace.ping_ms} ms)" if trace.ping_ms is not None else ""
            steps.append(Step(STEP_NAMES[3], StepStatus.OK, f"Lavalink est connecté au serveur vocal{ping}"))
        elif trace.closed:
            steps.append(Step(STEP_NAMES[3], StepStatus.FAILED, f"connexion vocale fermée, {trace.closed}"))
            problem = problem or f"Discord a fermé la connexion vocale de Lavalink : {trace.closed}."
        elif trace.voice_connected is False and age > START_GRACE_SECONDS:
            steps.append(Step(STEP_NAMES[3], StepStatus.FAILED, "Lavalink n'est pas connecté au serveur vocal"))
            problem = problem or (
                "Lavalink n'arrive pas à se connecter au serveur vocal de Discord. Causes fréquentes : Lavalink plus ancien "
                "que 4.2.0 (chiffrement DAVE), pare-feu qui bloque l'UDP sortant de Java, ou réseau qui bloque Discord."
            )
        elif age > AUDIO_GRACE_SECONDS:
            steps.append(Step(STEP_NAMES[3], StepStatus.FAILED, "Lavalink n'a envoyé aucun état du lecteur"))
            problem = problem or "Lavalink ne donne aucune nouvelle du lecteur : vérifiez que Lavalink fonctionne (/etat)."
        else:
            steps.append(Step(STEP_NAMES[3], StepStatus.WAITING, "en attente du premier état du lecteur (toutes les 5 s)"))

        # 5. Audio envoyé
        if trace.audio_seen_at is not None:
            steps.append(Step(STEP_NAMES[4], StepStatus.OK, "la lecture avance : Lavalink envoie le son à Discord"))
        elif paused:
            steps.append(Step(STEP_NAMES[4], StepStatus.WAITING, "lecture en pause"))
        elif trace.stuck:
            steps.append(Step(STEP_NAMES[4], StepStatus.FAILED, "lecture bloquée : aucune trame consommée"))
            problem = problem or (
                "La lecture est bloquée : Lavalink a le son mais ne peut pas l'envoyer (connexion vocale non opérationnelle)."
            )
        elif age > AUDIO_GRACE_SECONDS and trace.started_at is not None:
            steps.append(Step(STEP_NAMES[4], StepStatus.FAILED, "la position de lecture n'avance pas"))
            problem = problem or (
                "Le son ne part pas : la position de lecture n'avance pas alors que la piste a démarré. La connexion vocale "
                "de Lavalink ne fonctionne pas (voir docs/music.md, « Pas de son »)."
            )
        else:
            steps.append(Step(STEP_NAMES[4], StepStatus.WAITING, "en attente de deux états du lecteur"))

        if problem is None and lavalink is not None and lavalink.supports_dave is False:
            problem = lavalink.problems(())[0]
        return AudioReport(tuple(steps), problem)
