"""Pistes et entrées de la file d'attente (aucune dépendance à Discord ni à Lavalink)."""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Identifiants des entrées de la file : uniques dans le processus. Ils sont envoyés à Lavalink avec
# la piste (« userData ») et reviennent dans l'événement de fin : un événement d'une ancienne piste
# (même titre ajouté deux fois, /musique passer pendant la fin naturelle…) ne fait jamais avancer la file.
_entry_ids = itertools.count(1)


@dataclass(frozen=True)
class Track:
    """Piste résolue par le moteur audio, avec des métadonnées déjà nettoyées (voir ``search.normalize_track``)."""

    title: str
    author: str
    source: str  # nom de la source donné par Lavalink : youtube, soundcloud, spotify…
    identifier: str
    uri: str | None  # lien public de la piste (utilisé pour l'historique et pour rejouer un favori)
    length_ms: int
    is_stream: bool = False
    artwork_url: str | None = None
    # Données brutes du moteur audio (dont la piste encodée) : opaques pour tout le reste du code.
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def key(self) -> str:
        """Identité stable de la piste (favoris sans doublon)."""
        return f"{self.source}:{self.identifier}"


@dataclass(frozen=True)
class QueueEntry:
    """Une piste demandée par une personne, telle qu'elle est rangée dans la file."""

    track: Track
    requester_id: int
    requester_name: str
    entry_id: int = field(default_factory=lambda: next(_entry_ids))
    added_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC), compare=False)


@dataclass(frozen=True)
class SearchOutcome:
    """Résultat d'une recherche ou d'un lien, déjà filtré (durées, directs, limite des playlists)."""

    tracks: tuple[Track, ...]
    query_kind: str  # "search" (texte), "track" (lien d'une piste) ou "playlist"
    source_key: str  # clé de la source utilisée (voir search.SOURCES)
    playlist_name: str | None = None
    playlist_total: int = 0  # nombre de pistes de la playlist avant la limite
    rejected: int = 0  # pistes écartées (trop longues, directs, mal formées)

    @property
    def truncated(self) -> bool:
        return self.query_kind == "playlist" and self.playlist_total > len(self.tracks) + self.rejected
