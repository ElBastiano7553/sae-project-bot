"""Alertes en cas d'erreurs répétées du bot.

Chaque erreur journalisée (niveau ERROR) est enregistrée dans ``bot_errors``, regroupée
par empreinte : même source et même message, les nombres étant remplacés par « # »
(« salon 123 introuvable » et « salon 456 introuvable » sont la même erreur).

Une alerte n'est envoyée que si une même erreur se répète, pour ne pas déranger
l'équipe à chaque incident isolé :

* au moins ``threshold`` occurrences en ``window`` (3 en 10 minutes par défaut) ;
* au plus une alerte par erreur et par ``cooldown`` (1 heure) ;
* au plus ``max_per_hour`` alertes par heure, toutes erreurs confondues (5).

Les erreurs du module d'alerte lui-même ne déclenchent jamais d'alerte (pas de boucle).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from src.database.repositories import BotError, BotErrorRepository

THRESHOLD = 3
WINDOW = dt.timedelta(minutes=10)
COOLDOWN = dt.timedelta(hours=1)
MAX_PER_HOUR = 5
# Sources jamais alertées : l'envoi des alertes lui-même.
IGNORED_SOURCES = ("src.services.alerts", "src.discord_bot.alerts")
_DIGITS = re.compile(r"\d+")
SQLITE_FORMAT = "%Y-%m-%d %H:%M:%S"


def fingerprint(source: str, message: str) -> str:
    normalized = _DIGITS.sub("#", message.strip())[:200]
    return hashlib.sha256(f"{source}\n{normalized}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Alert:
    error: BotError
    occurrences: int  # dans la fenêtre courante
    window: dt.timedelta


class AlertManager:
    def __init__(
        self,
        repository: BotErrorRepository,
        *,
        threshold: int = THRESHOLD,
        window: dt.timedelta = WINDOW,
        cooldown: dt.timedelta = COOLDOWN,
        max_per_hour: int = MAX_PER_HOUR,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self.max_per_hour = max_per_hour
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._occurrences: dict[str, deque[dt.datetime]] = {}
        self._last_alert: dict[str, dt.datetime] = {}
        self._sent: deque[dt.datetime] = deque()

    def observe(self, source: str, message: str) -> Alert | None:
        """Enregistre l'erreur ; retourne l'alerte à envoyer, ou ``None``."""
        if source.startswith(IGNORED_SOURCES):
            return None
        now = self._clock()
        key = fingerprint(source, message)
        error = self.repository.record(key, source, message)
        recent = self._occurrences.setdefault(key, deque())
        recent.append(now)
        while recent and now - recent[0] > self.window:
            recent.popleft()
        if len(recent) < self.threshold:
            return None
        last = self._last_alert.get(key)
        if last is not None and now - last < self.cooldown:
            return None
        while self._sent and now - self._sent[0] > dt.timedelta(hours=1):
            self._sent.popleft()
        if len(self._sent) >= self.max_per_hour:
            return None
        self._last_alert[key] = now
        self._sent.append(now)
        self.repository.mark_alerted(key)
        return Alert(error, len(recent), self.window)
