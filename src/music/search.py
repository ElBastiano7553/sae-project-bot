"""Recherches et liens : sources prises en charge, validation des entrées, normalisation des pistes.

Sécurité : le bot n'envoie jamais à Lavalink une adresse arbitraire. Un texte est toujours
précédé d'un préfixe de recherche (« ytmsearch:… ») ; un lien n'est accepté que s'il désigne
un site d'une source activée (MUSIC_SOURCES), en https ou http, sans identifiants ni port,
ce qui exclut les adresses internes (127.0.0.1, 192.168.…, noms locaux). La configuration de
Lavalink fournie (deploy/lavalink/application.yml.example) désactive en plus les sources
« http » et « local » : deux protections indépendantes.

Ce module ne fait aucun appel réseau : il prépare l'identifiant envoyé au moteur audio
et nettoie ce que le moteur renvoie.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.music.errors import InvalidQuery
from src.music.models import Track

MAX_QUERY_LENGTH = 200
MAX_URL_LENGTH = 500
MAX_TITLE_LENGTH = 200
MAX_AUTHOR_LENGTH = 100
# Résultats proposés au choix pour une recherche par texte (menu Discord : 25 au plus).
SEARCH_CHOICES = 5


@dataclass(frozen=True)
class Source:
    key: str  # valeur de MUSIC_SOURCES
    label: str
    search_prefix: str  # préfixe de recherche de Lavalink ou du plugin
    hosts: frozenset[str]  # sites acceptés pour les liens (correspondance exacte)
    host_suffixes: tuple[str, ...] = ()  # sous-domaines acceptés (artiste.bandcamp.com)
    plugin: str = ""  # plugin Lavalink nécessaire (vide : intégré à Lavalink)
    # Vrai pour Spotify, Apple Music, Deezer : seules les MÉTADONNÉES viennent de ce service ;
    # le son est cherché et lu sur une autre source (YouTube, SoundCloud) par le plugin LavaSrc.
    mirrored: bool = False


_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"})

SOURCES: dict[str, Source] = {
    source.key: source
    for source in (
        Source("youtubemusic", "YouTube Music", "ytmsearch", _YOUTUBE_HOSTS, plugin="youtube-source"),
        Source("youtube", "YouTube", "ytsearch", _YOUTUBE_HOSTS, plugin="youtube-source"),
        Source(
            "soundcloud",
            "SoundCloud",
            "scsearch",
            frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}),
        ),
        Source("bandcamp", "Bandcamp", "bcsearch", frozenset({"bandcamp.com"}), host_suffixes=(".bandcamp.com",)),
        Source("spotify", "Spotify", "spsearch", frozenset({"open.spotify.com"}), plugin="LavaSrc", mirrored=True),
        Source("applemusic", "Apple Music", "amsearch", frozenset({"music.apple.com"}), plugin="LavaSrc", mirrored=True),
        Source(
            "deezer",
            "Deezer",
            "dzsearch",
            frozenset({"deezer.com", "www.deezer.com", "link.deezer.com"}),
            plugin="LavaSrc",
            mirrored=True,
        ),
    )
}
DEFAULT_SOURCES = ("youtubemusic", "soundcloud")

# Nom de source renvoyé par Lavalink (« sourceName ») → libellé affiché.
SOURCE_LABELS = {
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
    "bandcamp": "Bandcamp",
    "spotify": "Spotify",
    "applemusic": "Apple Music",
    "deezer": "Deezer",
}
MIRRORED_SOURCE_NAMES = frozenset({"spotify", "applemusic", "deezer"})

_CONTROL = re.compile(r"[\x00-\x1f\x7f​-‏ -‮⁦-⁩]")
_SPACES = re.compile(r"\s+")
_URL_LIKE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


@dataclass(frozen=True)
class PreparedQuery:
    """Ce qui sera demandé au moteur audio."""

    kind: str  # "search" ou "url"
    identifiers: tuple[str, ...]  # « ytmsearch:texte » pour chaque source, dans l'ordre ; ou le lien seul
    sources: tuple[str, ...]  # clés des sources correspondantes
    display: str  # texte nettoyé (pour les messages)


def clean_text(value: object, limit: int) -> str:
    """Texte d'une seule ligne, sans caractères de contrôle ni d'inversion de sens, borné à ``limit``."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = _SPACES.sub(" ", _CONTROL.sub(" ", text)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def parse_sources(raw: str) -> tuple[tuple[str, ...], list[str]]:
    """Lit MUSIC_SOURCES (« youtubemusic,soundcloud »). Retourne (sources, erreurs)."""
    keys: list[str] = []
    errors: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in SOURCES:
            errors.append(f"source inconnue {key!r} (valeurs possibles : {', '.join(SOURCES)})")
        elif key not in keys:
            keys.append(key)
    if not keys and not errors:
        errors.append("aucune source indiquée")
    return tuple(keys), errors


def host_source(host: str, enabled: Iterable[str]) -> str | None:
    """Source activée qui accepte ce site, ou ``None``."""
    host = host.lower().rstrip(".")
    for key in enabled:
        source = SOURCES[key]
        if host in source.hosts or any(host.endswith(suffix) for suffix in source.host_suffixes):
            return key
    return None


def validate_url(raw: str, enabled: Iterable[str]) -> tuple[str, str]:
    """Vérifie un lien. Retourne (lien nettoyé, clé de la source) ou lève ``InvalidQuery``."""
    enabled = tuple(enabled)
    if len(raw) > MAX_URL_LENGTH:
        raise InvalidQuery(f"Ce lien est trop long (plus de {MAX_URL_LENGTH} caractères).")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise InvalidQuery("Ce lien est mal formé.") from None
    if parts.scheme.lower() not in ("https", "http"):
        raise InvalidQuery("Seuls les liens https:// (ou http://) sont acceptés.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise InvalidQuery("Un lien contenant des identifiants n'est pas accepté.")
    if port is not None:
        raise InvalidQuery("Un lien avec un numéro de port n'est pas accepté.")
    host = (parts.hostname or "").lower()
    if not host or " " in raw:
        raise InvalidQuery("Ce lien est mal formé.")
    source = host_source(host, enabled)
    if source is None:
        labels = ", ".join(SOURCES[key].label for key in enabled)
        raise InvalidQuery(f"Ce site n'est pas pris en charge. Liens acceptés : {labels}. Vous pouvez aussi chercher par titre.")
    return raw, source


def prepare_query(raw: str, enabled: Iterable[str], *, source: str | None = None) -> PreparedQuery:
    """Nettoie la saisie de /play et /search et prépare les identifiants à demander au moteur audio.

    ``source`` : source choisie par l'utilisateur pour une recherche par texte (sinon l'ordre de MUSIC_SOURCES).
    """
    enabled = tuple(enabled)
    text = raw.strip()
    if not text:
        raise InvalidQuery("Indiquez un titre, un artiste ou un lien.")
    if _URL_LIKE.match(text):
        url, key = validate_url(text, enabled)
        return PreparedQuery("url", (url,), (key,), url)
    text = clean_text(text, MAX_QUERY_LENGTH + 1)
    if len(text) > MAX_QUERY_LENGTH:
        raise InvalidQuery(f"Recherche trop longue ({MAX_QUERY_LENGTH} caractères au plus).")
    if source is not None:
        if source not in enabled:
            raise InvalidQuery(f"La source {SOURCES[source].label if source in SOURCES else source} n'est pas activée sur ce bot.")
        order = (source,)
    else:
        order = enabled
    return PreparedQuery("search", tuple(f"{SOURCES[key].search_prefix}:{text}" for key in order), order, text)


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        return None
    return value


def normalize_track(data: Mapping[str, Any]) -> Track | None:
    """Piste au format Lavalink v4 (``{"encoded": …, "info": {…}}``) → ``Track``, ou ``None`` si inutilisable."""
    if not isinstance(data, Mapping):
        return None
    info = data.get("info")
    encoded = data.get("encoded")
    if not isinstance(info, Mapping) or not isinstance(encoded, str) or not encoded:
        return None
    identifier = info.get("identifier")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    length = info.get("length")
    length_ms = length if isinstance(length, int) and not isinstance(length, bool) and length >= 0 else 0
    source = clean_text(info.get("sourceName") or "inconnue", 40).lower()
    title = clean_text(info.get("title"), MAX_TITLE_LENGTH) or "Titre inconnu"
    return Track(
        title=title,
        author=clean_text(info.get("author"), MAX_AUTHOR_LENGTH),
        source=source,
        identifier=clean_text(identifier, 200),
        uri=_safe_url(info.get("uri")),
        length_ms=length_ms,
        is_stream=bool(info.get("isStream")),
        artwork_url=_safe_url(info.get("artworkUrl")),
        payload=dict(data),
    )


def source_label(source_name: str) -> str:
    """Libellé honnête : « Spotify (son lu sur une autre source) » pour une source à métadonnées seules."""
    label = SOURCE_LABELS.get(source_name, source_name.capitalize() or "Inconnue")
    if source_name in MIRRORED_SOURCE_NAMES:
        return f"{label} (son lu sur une autre source)"
    return label


def format_duration(ms: int | None) -> str:
    if ms is None or ms < 0:
        return "?"
    seconds = ms // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
