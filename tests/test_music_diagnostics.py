"""Diagnostic « pas de son » : les cinq étapes, la version de Lavalink et les codes de fermeture vocale.

Tests unitaires purs : ni Discord, ni Lavalink, ni réseau. Une horloge factice remplace le temps réel.
"""

from __future__ import annotations

import logging

from src.database.music import MusicRepository
from src.music.diagnostics import (
    AUDIO_GRACE_SECONDS,
    START_GRACE_SECONDS,
    AudioMonitor,
    LavalinkInfo,
    StepStatus,
    explain_close_code,
    parse_version,
)
from src.music.service import Listener, LoadResult, MusicConfig, MusicService
from tests.music_fakes import FakeBackend, raw_track

GUILD, OTHER = 42, 43


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def statuses(report) -> list[StepStatus]:
    return [step.status for step in report.steps]


# --- Version de Lavalink et codes de fermeture -------------------------------------------------


def test_parse_version():
    assert parse_version("4.2.2") == (4, 2, 2)
    assert parse_version("v4.0.8-rc1") == (4, 0, 8)
    assert parse_version("4.1") == (4, 1, 0)
    assert parse_version("abc") is None and parse_version(None) is None and parse_version("") is None


def test_lavalink_without_dave_is_reported():
    old = LavalinkInfo("4.0.8", ("youtube", "soundcloud"))
    assert old.supports_dave is False
    assert any("DAVE" in p for p in old.problems(("youtubemusic",)))
    assert LavalinkInfo("4.2.2", ("youtube",)).supports_dave is True
    assert LavalinkInfo("snapshot").supports_dave is None  # version illisible : pas de fausse alerte
    assert LavalinkInfo("snapshot").problems(("youtube",)) == []


def test_missing_sources_are_reported():
    info = LavalinkInfo("4.2.2", ("soundcloud", "http"))
    problems = info.problems(("youtubemusic", "soundcloud", "bandcamp"))
    assert any("youtube-plugin" in p for p in problems)
    assert any("bandcamp" in p for p in problems)
    assert not any("soundcloud" in p for p in problems)
    assert LavalinkInfo("4.2.2", ("youtube", "soundcloud")).problems(("youtube", "soundcloud")) == []


def test_close_codes_are_explained():
    assert "DAVE" in explain_close_code(4017)
    assert "4014" in explain_close_code(4014) and "déconnecté" in explain_close_code(4014)
    assert "inattendue" in explain_close_code(4999)
    assert "normale" in explain_close_code(1000)
    assert len(explain_close_code(4006, "x" * 500)) < 250  # raison tronquée


# --- Les cinq étapes ------------------------------------------------------------------------------


def test_nothing_playing_has_no_problem():
    monitor = AudioMonitor(Clock())
    report = monitor.report(GUILD, playing=False)
    assert report.problem is None and report.steps[0].status is StepStatus.UNKNOWN
    assert len(report.steps) == 5


def test_healthy_playback_reaches_all_five_steps():
    clock = Clock()
    monitor = AudioMonitor(clock)
    monitor.search_ok(GUILD, "youtubemusic")
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.started(GUILD, 1)
    monitor.player_update(GUILD, connected=True, ping_ms=30, position_ms=100, paused=False)
    clock.now += 5
    monitor.player_update(GUILD, connected=True, ping_ms=30, position_ms=5100, paused=False)
    report = monitor.report(GUILD, playing=True)
    assert report.all_ok and report.problem is None


def test_waiting_steps_are_not_failures_right_after_start():
    clock = Clock()
    monitor = AudioMonitor(clock)
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    clock.now += 2
    report = monitor.report(GUILD, playing=True)
    assert statuses(report)[2:] == [StepStatus.WAITING] * 3 and report.problem is None


def test_never_started_is_step_3():
    clock = Clock()
    monitor = AudioMonitor(clock)
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    clock.now += START_GRACE_SECONDS + 1
    report = monitor.report(GUILD, playing=True)
    assert report.steps[2].status is StepStatus.FAILED and "jamais démarré" in report.problem


def test_source_error_is_step_3():
    monitor = AudioMonitor(Clock())
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.track_failed(GUILD, 1, "This video is unavailable")
    report = monitor.report(GUILD, playing=True)
    assert report.steps[2].status is StepStatus.FAILED and "unavailable" in report.problem


def test_voice_never_connected_is_step_4():
    clock = Clock()
    monitor = AudioMonitor(clock)
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.started(GUILD, 1)
    monitor.player_update(GUILD, connected=False, ping_ms=-1, position_ms=0, paused=False)
    clock.now += START_GRACE_SECONDS + 1
    report = monitor.report(GUILD, playing=True)
    assert report.steps[2].status is StepStatus.OK
    assert report.steps[3].status is StepStatus.FAILED and "UDP" in report.problem


def test_dave_close_code_is_step_4():
    monitor = AudioMonitor(Clock())
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.started(GUILD, 1)
    monitor.voice_closed(GUILD, 4017, "E2EE/DAVE protocol required", by_remote=True)
    report = monitor.report(GUILD, playing=True)
    assert report.steps[3].status is StepStatus.FAILED and "DAVE" in report.problem


def test_position_not_moving_is_step_5():
    clock = Clock()
    monitor = AudioMonitor(clock)
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.started(GUILD, 1)
    monitor.player_update(GUILD, connected=True, ping_ms=20, position_ms=0, paused=False)
    clock.now += AUDIO_GRACE_SECONDS + 1
    monitor.player_update(GUILD, connected=True, ping_ms=20, position_ms=0, paused=False)
    report = monitor.report(GUILD, playing=True)
    assert statuses(report)[:4] == [StepStatus.UNKNOWN, StepStatus.OK, StepStatus.OK, StepStatus.OK]
    assert report.steps[4].status is StepStatus.FAILED and "n'avance pas" in report.problem


def test_position_moving_without_voice_does_not_count_as_sent():
    clock = Clock()
    monitor = AudioMonitor(clock)
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.started(GUILD, 1)
    monitor.player_update(GUILD, connected=False, ping_ms=-1, position_ms=0, paused=False)
    monitor.player_update(GUILD, connected=False, ping_ms=-1, position_ms=5000, paused=False)
    assert monitor.trace(GUILD).audio_seen_at is None


def test_paused_is_never_a_failure():
    clock = Clock()
    monitor = AudioMonitor(clock)
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.started(GUILD, 1)
    monitor.player_update(GUILD, connected=True, ping_ms=20, position_ms=0, paused=True)
    clock.now += AUDIO_GRACE_SECONDS + 5
    report = monitor.report(GUILD, playing=True, paused=True)
    assert report.steps[4].status is StepStatus.WAITING and report.problem is None


def test_stuck_track_is_step_5():
    monitor = AudioMonitor(Clock())
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    monitor.started(GUILD, 1)
    monitor.player_update(GUILD, connected=True, ping_ms=20, position_ms=0, paused=False)
    monitor.track_failed(GUILD, 1, "bloquée", stuck=True)
    report = monitor.report(GUILD, playing=True)
    assert report.steps[4].status is StepStatus.FAILED and "bloquée" in report.problem


def test_events_for_an_old_track_are_ignored_and_guilds_are_separate():
    monitor = AudioMonitor(Clock())
    monitor.loaded(GUILD, 2, "Nouveau", "youtube")
    assert monitor.started(GUILD, 1) is None  # ancienne piste : rien ne change
    assert monitor.trace(GUILD).started_at is None
    monitor.loaded(OTHER, 7, "Autre", "soundcloud")
    monitor.voice_closed(OTHER, 4014, "", by_remote=True)
    assert monitor.trace(GUILD).closed is None  # aucun mélange entre serveurs
    monitor.forget(OTHER)
    assert monitor.trace(OTHER) is None


def test_old_lavalink_is_the_problem_when_nothing_else_is_seen():
    monitor = AudioMonitor(Clock())
    monitor.loaded(GUILD, 1, "Titre", "youtube")
    report = monitor.report(GUILD, playing=True, lavalink=LavalinkInfo("4.0.8", ("youtube",)))
    assert "DAVE" in report.problem


# --- Intégration avec le service (faux moteur audio) ----------------------------------------------


def listener(user_id=1, channel=200):
    return Listener(user_id=user_id, name="Alice", voice_channel_id=channel, is_admin=False, has_dj_role=False, humans_in_channel=1)


async def started_service(db, clock):
    backend = FakeBackend()
    service = MusicService(backend, MusicRepository(db), MusicConfig(sources=("youtubemusic",)), audio_clock=clock)
    backend.results["ytmsearch:titre"] = LoadResult([raw_track("Titre", identifier="t")])
    outcome = await service.search(GUILD, 1, "titre")
    await service.enqueue(GUILD, listener(), list(outcome.tracks[:1]), text_channel_id=100)
    return service, backend


async def test_service_records_the_five_steps_and_logs_them(db, caplog):
    clock = Clock()
    service, _ = await started_service(db, clock)
    entry = service.snapshot(GUILD).current
    with caplog.at_level(logging.INFO, logger="src.music.service"):
        service.on_track_started(GUILD, entry.entry_id)
        service.on_player_update(GUILD, connected=True, ping_ms=25, position_ms=0)
        clock.now += 5
        service.on_player_update(GUILD, connected=True, ping_ms=25, position_ms=5000)
    report = service.audio_report(GUILD)
    assert report.all_ok
    for step in ("étape 3/5", "étape 4/5", "étape 5/5"):
        assert step in caplog.text
    assert service.check_audio(GUILD, entry.entry_id) is None  # rien d'anormal : panneau inchangé


async def test_service_check_audio_puts_the_cause_on_the_panel_once(db):
    clock = Clock()
    service, _ = await started_service(db, clock)
    entry = service.snapshot(GUILD).current
    service.on_track_started(GUILD, entry.entry_id)
    service.on_player_update(GUILD, connected=False, ping_ms=-1, position_ms=0)
    clock.now += AUDIO_GRACE_SECONDS + 1
    problem = service.check_audio(GUILD, entry.entry_id)
    assert problem and "serveur vocal" in problem
    assert service.players.get(GUILD).last_problem.startswith("Son :")
    assert service.check_audio(GUILD, entry.entry_id) is None  # une seule fois par piste
    assert service.check_audio(GUILD, entry.entry_id + 99) is None  # autre piste : rien


async def test_voice_closed_is_shown_on_the_panel(db):
    service, _ = await started_service(db, Clock())
    service.on_voice_closed(GUILD, 4017, "DAVE required", True)
    assert "DAVE" in service.players.get(GUILD).last_problem
    service.players.get(GUILD).last_problem = None
    service.on_voice_closed(GUILD, 1000, "", False)  # fermeture normale (départ) : rien d'affiché
    assert service.players.get(GUILD).last_problem is None


async def test_lavalink_problems_use_the_configured_sources(db):
    service, backend = await started_service(db, Clock())
    assert service.lavalink_problems() == []  # version encore inconnue
    backend.info = LavalinkInfo("4.0.8", ("soundcloud",))
    problems = service.lavalink_problems()
    assert any("DAVE" in p for p in problems) and any("YouTube" in p for p in problems)


async def test_leaving_forgets_the_trace(db):
    service, _ = await started_service(db, Clock())
    assert service.audio.trace(GUILD) is not None
    await service.leave(GUILD, None)
    assert service.audio.trace(GUILD) is None
