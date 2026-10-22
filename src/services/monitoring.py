"""Suivi des erreurs récentes et masquage des secrets dans les journaux.

``RecentErrors`` garde en mémoire les dernières erreurs journalisées (niveau ERROR),
réduites à une ligne et sans trace d'appel : ``/status`` affiche la plus récente.
``InteractionStats`` compte les commandes slash, leurs erreurs et leurs réponses
lentes depuis le démarrage (affiché par ``/status``).
``redact`` remplace les secrets connus (token Discord, secret des webhooks, clés
d'API) par ``***`` : il est appliqué à tous les journaux, console et fichier.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from src.services import text_limits as limits

MASK = "***"
# Un secret plus court ne peut pas être masqué sans risquer de masquer des mots ordinaires.
MIN_SECRET_LENGTH = 8


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, MASK)
    return text


@dataclass(frozen=True)
class ErrorRecord:
    at: dt.datetime
    source: str
    message: str


class RecentErrors:
    """Dernières erreurs du bot (mémoire uniquement : remise à zéro à chaque démarrage)."""

    def __init__(self, maxlen: int = 20) -> None:
        self._items: deque[ErrorRecord] = deque(maxlen=maxlen)
        self.total = 0
        self._listeners: list[Callable[[ErrorRecord], None]] = []
        self._notifying = False

    def add_listener(self, listener: Callable[[ErrorRecord], None]) -> None:
        """Appelé pour chaque nouvelle erreur (historique en base, alertes)."""
        self._listeners.append(listener)

    def record(self, source: str, message: str, at: dt.datetime | None = None) -> None:
        first = next((line.strip() for line in message.splitlines() if line.strip()), "erreur sans message")
        item = ErrorRecord(at or dt.datetime.now(dt.UTC), source, limits.truncate(first, 300))
        self._items.append(item)
        self.total += 1
        # Une erreur journalisée pendant le traitement d'une erreur n'est pas retransmise (pas de boucle).
        if self._notifying:
            return
        self._notifying = True
        try:
            for listener in self._listeners:
                try:
                    listener(item)
                except Exception:  # un suivi d'erreur ne doit jamais faire planter le bot
                    pass
        finally:
            self._notifying = False

    @property
    def last(self) -> ErrorRecord | None:
        return self._items[-1] if self._items else None

    def items(self) -> list[ErrorRecord]:
        return list(reversed(self._items))


class SecretRedactingFilter(logging.Filter):
    """Filtre de journalisation : masque les secrets dans le message final et dans la trace d'appel."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        cleaned = redact(message, self._secrets)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        return True


class ErrorTrackingHandler(logging.Handler):
    """Transmet les erreurs journalisées à ``RecentErrors`` (message seul, jamais la trace d'appel)."""

    def __init__(self, errors: RecentErrors, secrets: Iterable[str] = ()) -> None:
        super().__init__(level=logging.ERROR)
        self._errors = errors
        self._secrets = tuple(secrets)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(record.getMessage(), self._secrets)
            self._errors.record(record.name, message, dt.datetime.fromtimestamp(record.created, dt.UTC))
        except Exception:  # un journal ne doit jamais faire planter le bot
            self.handleError(record)


# Discord exige une première réponse en moins de 3 secondes : au-delà, l'utilisateur voit
# « L'application ne répond plus ». Une commande plus lente doit différer sa réponse.
SLOW_INTERACTION_MS = 2500


@dataclass(frozen=True)
class InteractionSummary:
    total: int
    errors: int
    slow: int
    median_ms: int | None
    slowest: str | None  # « /commande (1234 ms) »
    failing: str | None  # commande la plus souvent en erreur


class InteractionStats:
    """Mesures des commandes slash, en mémoire (remises à zéro à chaque démarrage).

    La durée est mesurée depuis la création de l'interaction par Discord jusqu'à la fin
    du traitement : elle comprend le trajet réseau, c'est ce que ressent l'utilisateur.
    """

    def __init__(self, keep: int = 200) -> None:
        self._durations: deque[int] = deque(maxlen=keep)
        self.total = 0
        self.errors: Counter[str] = Counter()
        self.slow = 0
        self._slowest: tuple[int, str] | None = None

    def record(self, command: str, duration_ms: float, *, ok: bool = True) -> bool:
        """Enregistre une commande terminée. Retourne ``True`` si elle a été lente."""
        duration = max(int(duration_ms), 0)
        self.total += 1
        self._durations.append(duration)
        if not ok:
            self.errors[command] += 1
        if self._slowest is None or duration > self._slowest[0]:
            self._slowest = (duration, command)
        slow = duration > SLOW_INTERACTION_MS
        self.slow += slow
        return slow

    def summary(self) -> InteractionSummary:
        durations = sorted(self._durations)
        median = durations[len(durations) // 2] if durations else None
        failing = self.errors.most_common(1)[0][0] if self.errors else None
        slowest = f"/{self._slowest[1]} ({self._slowest[0]} ms)" if self._slowest else None
        return InteractionSummary(self.total, sum(self.errors.values()), self.slow, median, slowest, failing)
