"""Recherches et liens : validation (dont les adresses internes), sources et normalisation des pistes."""

from __future__ import annotations

import pytest

from src.music.errors import InvalidQuery
from src.music.search import (
    DEFAULT_SOURCES,
    MAX_QUERY_LENGTH,
    SOURCES,
    clean_text,
    format_duration,
    normalize_track,
    parse_sources,
    prepare_query,
    source_label,
    validate_url,
)
from tests.music_fakes import raw_track

ENABLED = ("youtubemusic", "soundcloud")


def test_text_search_gets_a_prefix_for_each_enabled_source_in_order():
    query = prepare_query("  daft   punk\tone more time ", ENABLED)
    assert query.kind == "search"
    assert query.identifiers == ("ytmsearch:daft punk one more time", "scsearch:daft punk one more time")
    assert query.sources == ENABLED


def test_a_chosen_source_must_be_enabled():
    assert prepare_query("titre", ENABLED, source="soundcloud").identifiers == ("scsearch:titre",)
    with pytest.raises(InvalidQuery):
        prepare_query("titre", ENABLED, source="spotify")


def test_text_that_looks_like_a_lavalink_prefix_stays_a_search():
    # « local: » ou « http: » ne sont jamais envoyés tels quels : le texte est toujours préfixé.
    for raw in ("local:/etc/passwd", "ytsearch:x", "javascript:alert(1)"):
        query = prepare_query(raw, ENABLED)
        assert query.kind == "search"
        assert all(identifier.startswith(("ytmsearch:", "scsearch:")) for identifier in query.identifiers)


def test_empty_and_too_long_queries_are_refused():
    with pytest.raises(InvalidQuery):
        prepare_query("   ", ENABLED)
    with pytest.raises(InvalidQuery):
        prepare_query("a" * (MAX_QUERY_LENGTH + 1), ENABLED)


@pytest.mark.parametrize(
    "url, source",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtubemusic"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtubemusic"),
        ("https://music.youtube.com/watch?v=abc", "youtubemusic"),
        ("https://soundcloud.com/artiste/titre", "soundcloud"),
        ("http://on.soundcloud.com/xyz", "soundcloud"),
    ],
)
def test_supported_links_are_accepted(url, source):
    query = prepare_query(url, ENABLED)
    assert query.kind == "url" and query.identifiers == (url,) and query.sources == (source,)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:2333/v4/info",
        "http://localhost/admin",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://10.0.0.5/radio.mp3",
        "http://169.254.169.254/latest/meta-data/",
        "https://example.com/musique.mp3",
        "https://youtube.com.attaquant.fr/watch?v=x",
        "https://user:pass@www.youtube.com/watch?v=x",
        "https://www.youtube.com:8443/watch?v=x",
        "file:///etc/passwd",
        "ftp://soundcloud.com/x",
        "https://open.spotify.com/track/abc",  # Spotify non activé ici
    ],
)
def test_internal_and_unsupported_links_are_refused(url):
    with pytest.raises(InvalidQuery):
        prepare_query(url, ENABLED)


def test_bandcamp_subdomains_and_mirrored_sources_when_enabled():
    enabled = ("bandcamp", "spotify", "deezer", "applemusic")
    assert validate_url("https://artiste.bandcamp.com/track/x", enabled)[1] == "bandcamp"
    assert validate_url("https://open.spotify.com/track/abc", enabled)[1] == "spotify"
    assert validate_url("https://www.deezer.com/fr/track/1", enabled)[1] == "deezer"
    assert validate_url("https://music.apple.com/fr/album/x/1", enabled)[1] == "applemusic"
    with pytest.raises(InvalidQuery):
        validate_url("https://bandcamp.com.evil.org/x", enabled)


def test_mirrored_sources_are_never_presented_as_their_own_audio():
    assert all(SOURCES[key].mirrored for key in ("spotify", "applemusic", "deezer"))
    assert not any(SOURCES[key].mirrored for key in ("youtube", "youtubemusic", "soundcloud", "bandcamp"))
    assert "autre source" in source_label("spotify")
    assert source_label("soundcloud") == "SoundCloud"


def test_parse_sources():
    assert parse_sources("youtubemusic, soundcloud,youtubemusic") == (("youtubemusic", "soundcloud"), [])
    keys, errors = parse_sources("youtube,napster")
    assert keys == ("youtube",) and errors and "napster" in errors[0]
    assert parse_sources(" , ")[1]
    assert set(DEFAULT_SOURCES) <= set(SOURCES)


def test_normalize_track_cleans_metadata():
    data = raw_track("  Titre‮ inversé\n**gras** ", author="A" * 300, identifier="abc")
    track = normalize_track(data)
    assert track.title == "Titre inversé **gras**"  # contrôle et inversion de sens retirés ; le Markdown est neutralisé à l'affichage
    assert len(track.author) == 100 and track.author.endswith("…")
    assert track.key == "youtube:abc"
    assert track.payload["encoded"] == "ENC-abc"


@pytest.mark.parametrize(
    "data",
    [
        None,
        "texte",
        {},
        {"encoded": "x"},
        {"encoded": "", "info": {"identifier": "a"}},
        {"encoded": "x", "info": {"identifier": ""}},
        {"encoded": 3, "info": {"identifier": "a"}},
    ],
)
def test_malformed_tracks_are_ignored(data):
    assert normalize_track(data) is None


def test_malformed_fields_get_safe_values():
    data = raw_track(identifier="z")
    data["info"].update(length=-5, title=None, uri="javascript:alert(1)", artworkUrl="http://user:pw@x.fr/a.png", sourceName=None)
    track = normalize_track(data)
    assert track.length_ms == 0 and track.title == "Titre inconnu" and track.uri is None and track.artwork_url is None
    assert track.source == "inconnue"
    data["info"]["length"] = True  # un booléen n'est pas une durée
    assert normalize_track(data).length_ms == 0


def test_clean_text_and_durations():
    assert clean_text("a\x00b​c", 10) == "a b c"
    assert format_duration(0) == "0:00"
    assert format_duration(61_000) == "1:01"
    assert format_duration(3_723_000) == "1:02:03"
    assert format_duration(None) == "?"
