"""File d'attente d'UN serveur : FIFO bornée (MUSIC_MAX_QUEUE), sans accès réseau ni Discord.

La file n'est pas protégée contre les accès concurrents par elle-même : c'est le lecteur
(``player.GuildPlayer``) qui la manipule, toujours sous son verrou.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable, Iterator

from src.music.errors import QueueFull
from src.music.models import QueueEntry


class TrackQueue:
    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError("max_size doit être ≥ 1")
        self.max_size = max_size
        self._items: deque[QueueEntry] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[QueueEntry]:
        return iter(tuple(self._items))  # copie : la file peut changer pendant qu'on l'affiche

    def __bool__(self) -> bool:
        return bool(self._items)

    @property
    def remaining(self) -> int:
        return self.max_size - len(self._items)

    def add(self, entry: QueueEntry) -> int:
        """Ajoute une piste à la fin. Retourne sa position (1 = la prochaine). Lève ``QueueFull``."""
        if self.remaining <= 0:
            raise QueueFull(f"La file est pleine ({self.max_size} titres au plus). Attendez qu'elle avance un peu.")
        self._items.append(entry)
        return len(self._items)

    def extend(self, entries: Iterable[QueueEntry]) -> list[QueueEntry]:
        """Ajoute autant de pistes que la place le permet, dans l'ordre. Retourne celles qui ont été ajoutées."""
        added: list[QueueEntry] = []
        for entry in entries:
            if self.remaining <= 0:
                break
            self._items.append(entry)
            added.append(entry)
        if not added and self.remaining <= 0:
            raise QueueFull(f"La file est pleine ({self.max_size} titres au plus). Attendez qu'elle avance un peu.")
        return added

    def push_front(self, entry: QueueEntry) -> bool:
        """Remet une piste en tête (répétition d'un titre). Retourne False si la file est pleine."""
        if self.remaining <= 0:
            return False
        self._items.appendleft(entry)
        return True

    def append_if_room(self, entry: QueueEntry) -> bool:
        """Ajoute en fin de file s'il reste de la place (répétition de la file). Ne lève jamais."""
        if self.remaining <= 0:
            return False
        self._items.append(entry)
        return True

    def peek_at(self, position: int) -> QueueEntry | None:
        return self._items[position - 1] if 1 <= position <= len(self._items) else None

    def remove_at(self, position: int) -> QueueEntry | None:
        """Retire la piste à la position ``position`` (1 = la prochaine). ``None`` si la position n'existe pas."""
        if not 1 <= position <= len(self._items):
            return None
        entry = self._items[position - 1]
        del self._items[position - 1]
        return entry

    def shuffle(self, rng: random.Random | None = None) -> int:
        """Mélange les pistes en attente (le titre en cours n'est pas dans la file). Retourne leur nombre."""
        items = list(self._items)
        (rng or random).shuffle(items)
        self._items = deque(items)
        return len(items)

    def pop_next(self) -> QueueEntry | None:
        return self._items.popleft() if self._items else None

    def peek(self) -> QueueEntry | None:
        return self._items[0] if self._items else None

    def position_of(self, entry_id: int) -> int | None:
        for index, entry in enumerate(self._items, start=1):
            if entry.entry_id == entry_id:
                return index
        return None

    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        return count

    def total_length_ms(self) -> int:
        return sum(entry.track.length_ms for entry in self._items)

    def page(self, page: int, per_page: int) -> tuple[list[tuple[int, QueueEntry]], int]:
        """Page ``page`` (à partir de 1) : [(position, entrée)…] et nombre total de pages (au moins 1)."""
        pages = max(1, -(-len(self._items) // per_page))
        page = min(max(page, 1), pages)
        start = (page - 1) * per_page
        items = list(self._items)[start : start + per_page]
        return [(start + offset + 1, entry) for offset, entry in enumerate(items)], pages
