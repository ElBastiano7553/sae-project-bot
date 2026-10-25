"""Service de musique avec un faux moteur audio : file, transitions, erreurs, concurrence, volume, favoris."""

from __future__ import annotations

import asyncio

import pytest

from src.music import service as service_module
from src.music.errors import (
    BackendError,
    ControlDenied,
    InvalidQuery,
    MusicUnavailable,
    NoResults,
    NothingPlaying,
    QueueFull,
    SlowDown,
    SourceUnavailable,
    StaleControl,
    WrongVoiceChannel,
)
from src.music.player import PlayerState
from src.music.search import normalize_track
from src.music.service import MAX_CONSECUTIVE_FAILURES, LoadResult
from tests.music_fakes import GUILD_A, GUILD_B, TEXT_A, VOICE_A, VOICE_A2, VOICE_B, FakeBackend, listener, make_service, raw_track


def track(title="T", **kwargs):
    return normalize_track(raw_track(title, **kwargs))


@pytest.fixture
def setup(db):
    return make_service(db)


async def play(service, *titles, guild=GUILD_A, who=None):
    who = who or listener()
    return await service.enqueue(guild, who, [track(t, identifier=f"{guild}-{t}") for t in titles], text_channel_id=TEXT_A)


# --- Ajout et lecture -----------------------------------------------------------------------


async def test_first_track_connects_and_plays_then_others_queue(setup):
    service, backend, changes = setup
    result = await play(service, "a")
    assert result.connected and result.started.track.title == "a" and result.first_position == 0
    assert backend.connected == {GUILD_A: VOICE_A}
    result = await play(service, "b", "c")
    assert result.started is None and result.first_position == 1 and len(result.added) == 2
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.PLAYING and [e.track.title for e in snap.upcoming] == ["b", "c"]
    assert backend.count("connect") == 1 and backend.count("play") == 1  # pas d'interruption du titre en cours
    assert changes == [GUILD_A, GUILD_A]


async def test_track_end_advances_in_fifo_order_and_records_history(setup, db):
    service, backend, _ = setup
    await play(service, "a", "b", "c")
    for expected in ("b", "c"):
        current = service.snapshot(GUILD_A).current
        started = await service.on_track_end(GUILD_A, current.entry_id, "finished")
        assert started.track.title == expected
    last = service.snapshot(GUILD_A).current
    assert await service.on_track_end(GUILD_A, last.entry_id, "finished") is None
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.IDLE and snap.current is None and snap.voice_channel_id == VOICE_A
    rows, total = service.history(GUILD_A, limit=10)
    assert total == 3 and [r.title for r in rows] == ["c", "b", "a"]


async def test_stale_end_events_never_advance_twice(setup):
    service, backend, _ = setup
    await play(service, "a", "b", "c")
    first = service.snapshot(GUILD_A).current
    await service.skip(GUILD_A, listener())  # « a » remplacé par « b »
    # Lavalink signale ensuite la fin de « a » : remplacée, puis même un « finished » tardif.
    assert await service.on_track_end(GUILD_A, first.entry_id, "replaced") is None
    assert await service.on_track_end(GUILD_A, first.entry_id, "finished") is None
    assert service.snapshot(GUILD_A).current.track.title == "b"
    assert await service.on_track_end(GUILD_A, None, "finished") is None  # événement sans identifiant
    assert await service.on_track_end(GUILD_B, first.entry_id, "finished") is None  # autre serveur


async def test_same_track_twice_in_a_row_uses_entry_ids(setup):
    service, backend, _ = setup
    same = track("même", identifier="x")
    await service.enqueue(GUILD_A, listener(), [same, same, same])
    first = service.snapshot(GUILD_A).current
    await service.skip(GUILD_A, listener())
    second = service.snapshot(GUILD_A).current
    assert first.track == second.track and first.entry_id != second.entry_id
    assert await service.on_track_end(GUILD_A, first.entry_id, "finished") is None  # ancienne entrée : ignorée
    assert service.snapshot(GUILD_A).current.entry_id == second.entry_id


async def test_concurrent_skip_and_track_end_make_a_single_transition(setup):
    service, backend, _ = setup
    await play(service, "a", "b", "c", "d")
    current = service.snapshot(GUILD_A).current
    await asyncio.gather(
        service.on_track_end(GUILD_A, current.entry_id, "finished"),
        service.skip(GUILD_A, listener(), session_id=service.snapshot(GUILD_A).session_id, entry_id=current.entry_id),
        return_exceptions=True,
    )
    snap = service.snapshot(GUILD_A)
    assert snap.current.track.title == "b" and [e.track.title for e in snap.upcoming] == ["c", "d"]
    assert backend.count("play") == 2


async def test_cleanup_and_ignored_reasons(setup):
    service, _, _ = setup
    await play(service, "a", "b")
    current = service.snapshot(GUILD_A).current
    assert await service.on_track_end(GUILD_A, current.entry_id, "stopped") is None
    assert service.snapshot(GUILD_A).current is current
    await service.on_track_end(GUILD_A, current.entry_id, "cleanup")
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.DISCONNECTED and not snap.upcoming


async def test_unreadable_tracks_are_skipped_then_playback_stops(setup):
    service, backend, _ = setup
    backend.fail_play = {f"{GUILD_A}-a", f"{GUILD_A}-b"}
    result = await play(service, "a", "b", "c")
    assert result.started.track.title == "c"
    assert "n'a pas pu être lu" in service.snapshot(GUILD_A).last_problem
    # Échecs signalés par Lavalink pendant la lecture (« loadFailed ») : arrêt après le maximum.
    titles = [f"x{i}" for i in range(MAX_CONSECUTIVE_FAILURES + 2)]
    await play(service, *titles)
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        current = service.snapshot(GUILD_A).current
        if current is None:
            break
        await service.on_track_end(GUILD_A, current.entry_id, "loadFailed")
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.IDLE and not snap.upcoming
    assert "échecs d'affilée" in snap.last_problem


async def test_play_failures_stop_after_the_maximum(setup):
    service, backend, _ = setup
    backend.fail_play = {f"{GUILD_A}-{t}" for t in "abcd"}
    result = await play(service, "a", "b", "c", "d", "e")
    assert result.started is None
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.IDLE and snap.upcoming == ()  # « e » n'a pas été tenté : la file est vidée
    assert backend.count("play") == MAX_CONSECUTIVE_FAILURES


async def test_stuck_track_is_skipped(setup):
    service, _, _ = setup
    await play(service, "a", "b")
    current = service.snapshot(GUILD_A).current
    started = await service.on_track_problem(GUILD_A, current.entry_id, "bloquée", stuck=True)
    assert started.track.title == "b" and "bloqué" in service.snapshot(GUILD_A).last_problem
    # Une exception seule ne change rien : la fin « loadFailed » qui suit fait avancer la file.
    assert await service.on_track_problem(GUILD_A, started.entry_id, "erreur", stuck=False) is None


async def test_queue_limit_and_partial_playlist(db):
    service, _, _ = make_service(db, max_queue=3)
    result = await play(service, "a", "b", "c", "d", "e")  # « a » lancé, 3 en file, 1 refusé
    assert result.not_added == 1 and len(service.snapshot(GUILD_A).upcoming) == 3
    with pytest.raises(QueueFull):
        await play(service, "f")


# --- Contrôles --------------------------------------------------------------------------


async def test_pause_resume_and_toggle_follow_the_real_state(setup):
    service, backend, _ = setup
    await play(service, "a")
    assert await service.set_paused(GUILD_A, listener(), True)
    assert not await service.set_paused(GUILD_A, listener(), True)  # déjà en pause : rien envoyé
    assert backend.count("pause") == 1
    assert await service.toggle_pause(GUILD_A, listener()) is False  # était en pause → reprise
    assert service.snapshot(GUILD_A).state is PlayerState.PLAYING
    assert await service.toggle_pause(GUILD_A, listener()) is True


async def test_skip_while_paused_starts_next_track(setup):
    service, _, _ = setup
    await play(service, "a", "b")
    await service.set_paused(GUILD_A, listener(), True)
    _, started = await service.skip(GUILD_A, listener())
    assert started.track.title == "b" and service.snapshot(GUILD_A).state is PlayerState.PLAYING


async def test_skip_last_track_stops(setup):
    service, backend, _ = setup
    await play(service, "a")
    skipped, started = await service.skip(GUILD_A, listener())
    assert skipped.track.title == "a" and started is None and backend.count("stop") == 1
    assert service.snapshot(GUILD_A).state is PlayerState.IDLE
    with pytest.raises(NothingPlaying):
        await service.skip(GUILD_A, listener())


async def test_stop_clears_queue_but_keeps_history_and_favorites(setup):
    service, backend, _ = setup
    await play(service, "a", "b", "c")
    track_a, added = service.toggle_favorite(GUILD_A, 1)
    assert added
    assert await service.stop(GUILD_A, listener()) == 2
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.IDLE and snap.current is None and not snap.upcoming
    assert service.history(GUILD_A, limit=10)[1] == 1
    assert [f.title for f in service.favorites(GUILD_A, 1)] == ["a"]


async def test_leave_disconnects_and_ends_the_session(setup):
    service, backend, _ = setup
    await play(service, "a", "b")
    session = service.snapshot(GUILD_A).session_id
    assert await service.leave(GUILD_A, listener()) == 1
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.DISCONNECTED and snap.session_id == "" and GUILD_A not in backend.connected
    with pytest.raises(NothingPlaying):
        await service.set_paused(GUILD_A, listener(), True, session_id=session)
    result = await play(service, "c")  # nouvelle session
    assert result.connected and service.snapshot(GUILD_A).session_id != session


async def test_leave_resets_even_if_lavalink_fails(setup):
    service, backend, _ = setup
    await play(service, "a")

    async def broken(guild_id):
        raise BackendError("perdu")

    backend.disconnect = broken
    await service.leave(GUILD_A, None)
    assert service.snapshot(GUILD_A).state is PlayerState.DISCONNECTED


async def test_controls_check_session_entry_channel_and_role(db):
    service, _, _ = make_service(db, dj_role_configured=True)
    await play(service, "a", "b", who=listener(1))
    snap = service.snapshot(GUILD_A)
    with pytest.raises(StaleControl):
        await service.skip(GUILD_A, listener(1), session_id="deadbeef", entry_id=snap.current.entry_id)
    with pytest.raises(StaleControl):
        await service.skip(GUILD_A, listener(1), session_id=snap.session_id, entry_id=snap.current.entry_id + 999)
    with pytest.raises(WrongVoiceChannel):
        await service.set_paused(GUILD_A, listener(2, voice=VOICE_A2), True)
    with pytest.raises(ControlDenied):
        await service.skip(GUILD_A, listener(2))  # titre d'une autre personne, sans rôle DJ
    with pytest.raises(ControlDenied):
        await service.stop(GUILD_A, listener(2))
    with pytest.raises(ControlDenied):
        await service.set_volume(GUILD_A, listener(2), 10)
    await service.skip(GUILD_A, listener(1), session_id=snap.session_id, entry_id=snap.current.entry_id)
    assert await service.stop(GUILD_A, listener(3, voice=None, admin=True)) == 0  # administrateur hors du salon


async def test_volume_range_persistence_and_concurrency(setup, db):
    service, backend, _ = setup
    await play(service, "a")
    for bad in (-1, 101, 1000):
        with pytest.raises(InvalidQuery):
            await service.set_volume(GUILD_A, listener(), bad)
    await asyncio.gather(*(service.set_volume(GUILD_A, listener(), v) for v in (10, 20, 30)))
    assert service.snapshot(GUILD_A).volume == backend.volumes[GUILD_A] == service.repo.volume(GUILD_A) == 30
    # Le volume du serveur est retrouvé par un nouveau service (après un redémarrage du bot).
    restarted, backend2, _ = make_service(db)
    await play(restarted, "b")
    assert backend2.volumes[GUILD_A] == 30
    with pytest.raises(NothingPlaying):
        await service.set_volume(GUILD_B, listener(voice=VOICE_B), 10)


async def test_guilds_are_fully_isolated(setup):
    service, backend, _ = setup
    await play(service, "a", "b", guild=GUILD_A, who=listener(1, voice=VOICE_A))
    await play(service, "x", guild=GUILD_B, who=listener(2, voice=VOICE_B))
    await service.stop(GUILD_B, listener(2, voice=VOICE_B))
    a, b = service.snapshot(GUILD_A), service.snapshot(GUILD_B)
    assert a.current.track.title == "a" and len(a.upcoming) == 1 and b.current is None
    service.toggle_favorite(GUILD_A, 1)
    assert service.favorites(GUILD_B, 1) == [] and service.history(GUILD_B, limit=10)[1] == 1
    # Un événement de fin portant l'identifiant d'une entrée du serveur A, reçu pour B, est ignoré.
    assert await service.on_track_end(GUILD_B, a.current.entry_id, "finished") is None
    assert service.snapshot(GUILD_A).current.track.title == "a"


async def test_voice_moves_and_lavalink_loss(setup):
    service, _, changes = setup
    await play(service, "a", "b")
    await service.on_voice_moved(GUILD_A, VOICE_A2)
    assert service.snapshot(GUILD_A).voice_channel_id == VOICE_A2
    await service.on_voice_moved(GUILD_A, None)  # bot sorti du salon par quelqu'un
    assert service.snapshot(GUILD_A).state is PlayerState.DISCONNECTED
    await play(service, "c", who=listener(voice=VOICE_A))
    assert await service.reset_all("Lavalink perdu") == 1
    snap = service.snapshot(GUILD_A)
    assert snap.state is PlayerState.DISCONNECTED and snap.last_problem == "Lavalink perdu"


async def test_unavailable_backend(setup):
    service, backend, _ = setup
    backend.ready = False
    with pytest.raises(MusicUnavailable):
        await service.search(GUILD_A, 1, "titre")
    with pytest.raises(MusicUnavailable):
        await play(service, "a")


async def test_backend_timeouts_become_clear_errors(setup, monkeypatch):
    service, backend, _ = setup
    monkeypatch.setattr(service_module, "COMMAND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(service_module, "SEARCH_TIMEOUT_SECONDS", 0.05)
    backend.hang = {"connect"}
    with pytest.raises(SourceUnavailable):
        await play(service, "a")
    assert service.snapshot(GUILD_A).state is PlayerState.DISCONNECTED  # rien n'a changé
    backend.hang = {"load"}
    with pytest.raises(SourceUnavailable):
        await service.search(GUILD_A, 1, "titre")


async def test_not_in_voice(setup):
    service, _, _ = setup
    from src.music.errors import NotInVoice

    with pytest.raises(NotInVoice):
        await play(service, "a", who=listener(voice=None))


# --- Recherche ----------------------------------------------------------------------------


async def test_search_falls_back_to_the_next_source(setup):
    service, backend, _ = setup
    backend.results["ytmsearch:titre"] = BackendError("bloqué par YouTube")
    backend.results["scsearch:titre"] = LoadResult([raw_track(f"r{i}", source="soundcloud") for i in range(8)])
    outcome = await service.search(GUILD_A, 1, "titre")
    assert outcome.source_key == "soundcloud" and outcome.query_kind == "search" and len(outcome.tracks) == 5
    assert [c[1] for c in backend.calls if c[0] == "load"] == ["ytmsearch:titre", "scsearch:titre"]


async def test_search_errors(setup):
    service, backend, _ = setup
    with pytest.raises(NoResults):
        await service.search(GUILD_A, 1, "rien")
    backend.results["ytmsearch:panne"] = BackendError("x")
    backend.results["scsearch:panne"] = BackendError("x")
    with pytest.raises(SourceUnavailable):
        await service.search(GUILD_A, 1, "panne")
    with pytest.raises(InvalidQuery):
        await service.search(GUILD_A, 1, "http://127.0.0.1:2333/v4/info")
    assert all(call[1] != "http://127.0.0.1:2333/v4/info" for call in backend.calls if call[0] == "load")


async def test_long_tracks_and_streams_are_rejected(db):
    service, backend, _ = make_service(db, max_track_minutes=10)
    backend.results["ytmsearch:long"] = LoadResult([raw_track("long", length=11 * 60_000), raw_track("direct", stream=True)])
    backend.results["scsearch:long"] = LoadResult([])
    with pytest.raises(NoResults, match="aucun n'est accepté"):
        await service.search(GUILD_A, 1, "long")
    backend.results["ytmsearch:mix"] = LoadResult([raw_track("long", length=11 * 60_000), raw_track("ok", length=60_000)])
    outcome = await service.search(GUILD_A, 1, "mix")
    assert [t.title for t in outcome.tracks] == ["ok"] and outcome.rejected == 1


async def test_playlists_are_truncated_to_the_limit(db):
    service, backend, _ = make_service(db, max_playlist=3)
    url = "https://www.youtube.com/playlist?list=PL123"
    backend.results[url] = LoadResult([raw_track(f"p{i}") for i in range(40)], playlist_name="Ma liste")
    outcome = await service.search(GUILD_A, 1, url)
    assert outcome.query_kind == "playlist" and len(outcome.tracks) == 3 and outcome.truncated
    assert outcome.playlist_total == 40 and outcome.playlist_name == "Ma liste"
    single = "https://www.youtube.com/watch?v=abc"
    backend.results[single] = LoadResult([raw_track("seul")])
    assert (await service.search(GUILD_A, 1, single)).query_kind == "track"


async def test_search_cooldown_and_parallel_limit(db):
    service, backend, _ = make_service(db)
    service._clock = lambda: 100.0  # horloge figée : deux recherches immédiates
    backend.results["ytmsearch:a"] = LoadResult([raw_track("a")])
    await service.search(GUILD_A, 1, "a")
    with pytest.raises(SlowDown):
        await service.search(GUILD_A, 1, "a")
    await service.search(GUILD_A, 2, "a")  # une autre personne n'est pas concernée
    # Au plus deux recherches simultanées par serveur.
    service2, backend2, _ = make_service(db)
    backend2.hang = {"load"}
    tasks = [asyncio.create_task(service2.search(GUILD_A, user, "x")) for user in (1, 2)]
    await asyncio.sleep(0)
    with pytest.raises(SlowDown):
        await service2.search(GUILD_A, 3, "x")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_resolve_uri_revalidates_the_link(setup):
    service, backend, _ = setup
    with pytest.raises(InvalidQuery):
        await service.resolve_uri(GUILD_A, 1, "http://10.0.0.1/x")
    url = "https://soundcloud.com/a/b"
    backend.results[url] = LoadResult([raw_track("b", source="soundcloud")])
    assert (await service.resolve_uri(GUILD_A, 1, url)).title == "b"


# --- Favoris ------------------------------------------------------------------------------


async def test_favorites_toggle_without_duplicates_and_removal_by_number(setup):
    service, _, _ = setup
    await play(service, "a")
    assert service.toggle_favorite(GUILD_A, 1)[1] is True
    assert service.toggle_favorite(GUILD_A, 1)[1] is False  # deuxième appui : retiré
    assert service.favorites(GUILD_A, 1) == []
    service.toggle_favorite(GUILD_A, 1)
    service.toggle_favorite(GUILD_A, 2)
    assert len(service.favorites(GUILD_A, 1)) == 1 and len(service.favorites(GUILD_A, 2)) == 1
    assert service.remove_favorite_at(GUILD_A, 1, 1).title == "a"
    with pytest.raises(InvalidQuery):
        service.remove_favorite_at(GUILD_A, 1, 1)


async def test_favorite_requires_a_current_track_with_a_link(setup):
    service, _, _ = setup
    with pytest.raises(NothingPlaying):
        service.toggle_favorite(GUILD_A, 1)
    await service.enqueue(GUILD_A, listener(), [track("sans lien", uri=None)])
    with pytest.raises(InvalidQuery):
        service.toggle_favorite(GUILD_A, 1)
    snap = service.snapshot(GUILD_A)
    with pytest.raises(StaleControl):
        service.toggle_favorite(GUILD_A, 1, session_id=snap.session_id, entry_id=snap.current.entry_id + 1)


async def test_history_survives_a_failing_repository(setup, caplog):
    service, backend, _ = setup

    def broken(*args, **kwargs):
        raise RuntimeError("disque plein")

    service.repo.record_play = broken
    result = await play(service, "a")
    assert result.started is not None  # la lecture ne dépend pas de l'historique
    assert "Historique musical non enregistré" in caplog.text


async def test_shutdown_disconnects_every_guild_and_leaves_no_task(setup):
    service, backend, _ = setup
    await play(service, "a", guild=GUILD_A, who=listener(voice=VOICE_A))
    await play(service, "b", guild=GUILD_B, who=listener(voice=VOICE_B))
    await service.shutdown()
    assert backend.connected == {} and all(not p.connected for p in service.players.all())
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert pending == []


async def test_on_change_errors_are_contained(db, caplog):
    service, backend, _ = make_service(db)

    def broken(guild_id):
        raise RuntimeError("panneau")

    service.on_change = broken
    await play(service, "a")
    assert "Actualisation du panneau de musique impossible" in caplog.text


def test_backend_protocol_is_satisfied_by_the_fake():
    backend = FakeBackend()
    for name in ("load", "connect", "play", "set_paused", "stop", "set_volume", "disconnect", "position_ms"):
        assert callable(getattr(backend, name))
