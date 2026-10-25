"""File d'attente, états du lecteur, isolation des serveurs et règles d'autorisation (sans Discord ni Lavalink)."""

from __future__ import annotations

import pytest

from src.music.errors import ControlDenied, NotInVoice, QueueFull, WrongVoiceChannel
from src.music.models import QueueEntry
from src.music.permissions import MusicPolicy
from src.music.player import GuildPlayer, InvalidTransition, PlayerManager, PlayerState
from src.music.queue import TrackQueue
from src.music.search import normalize_track
from tests.music_fakes import GUILD_A, GUILD_B, VOICE_A, VOICE_A2, listener, raw_track


def entry(title: str = "T", requester: int = 1) -> QueueEntry:
    return QueueEntry(normalize_track(raw_track(title)), requester, "Alice")


# --- File ----------------------------------------------------------------------------


def test_queue_is_fifo():
    queue = TrackQueue(10)
    first, second, third = entry("1"), entry("2"), entry("3")
    assert [queue.add(first), queue.add(second), queue.add(third)] == [1, 2, 3]
    assert queue.peek() is first
    assert [queue.pop_next(), queue.pop_next(), queue.pop_next()] == [first, second, third]
    assert queue.pop_next() is None and not queue


def test_queue_limits():
    queue = TrackQueue(2)
    queue.add(entry())
    added = queue.extend([entry(), entry(), entry()])
    assert len(added) == 1 and len(queue) == 2 and queue.remaining == 0
    with pytest.raises(QueueFull):
        queue.add(entry())
    with pytest.raises(QueueFull):
        queue.extend([entry()])
    with pytest.raises(ValueError):
        TrackQueue(0)


def test_queue_pages_positions_and_clear():
    queue = TrackQueue(50)
    entries = [entry(str(i)) for i in range(23)]
    queue.extend(entries)
    page, pages = queue.page(3, 10)
    assert pages == 3 and [p for p, _ in page] == [21, 22, 23]
    assert queue.page(99, 10)[0][0][0] == 21  # page trop grande : dernière page
    assert queue.position_of(entries[4].entry_id) == 5 and queue.position_of(-1) is None
    assert queue.total_length_ms() == 23 * 200_000
    snapshot = list(queue)
    assert queue.clear() == 23 and len(snapshot) == 23  # l'itération est une copie
    assert TrackQueue(5).page(1, 10) == ([], 1)


def test_entry_ids_are_unique():
    assert len({entry().entry_id for _ in range(100)}) == 100


# --- États du lecteur ------------------------------------------------------------------


def test_valid_transitions_and_sessions():
    player = GuildPlayer(GUILD_A, max_queue=5, volume=50)
    assert player.state is PlayerState.DISCONNECTED and not player.connected
    first_session = player.connected_to(VOICE_A)
    assert player.state is PlayerState.IDLE and len(first_session) == 8
    e = entry()
    player.started(e)
    assert player.state is PlayerState.PLAYING and player.current is e
    player.paused()
    player.resumed()
    player.started(entry())  # piste suivante : PLAYING → PLAYING
    player.finished()
    assert player.state is PlayerState.IDLE and player.current is None
    player.queue.add(entry())
    assert player.disconnected() == 1 and player.state is PlayerState.DISCONNECTED and player.session_id == ""
    assert player.connected_to(VOICE_A) != first_session  # nouvelle session : les anciens boutons ne marchent plus


@pytest.mark.parametrize(
    "setup, action",
    [
        ([], "started"),
        ([], "paused"),
        (["connect"], "paused"),
        (["connect"], "resumed"),
        (["connect", "start"], "resumed"),
        (["connect", "start", "pause"], "paused"),
        (["connect", "start", "pause"], "finished_then_resumed"),
    ],
)
def test_invalid_transitions_are_refused(setup, action):
    player = GuildPlayer(GUILD_A, max_queue=5, volume=50)
    for step in setup:
        {"connect": lambda: player.connected_to(VOICE_A), "start": lambda: player.started(entry()), "pause": player.paused}[step]()
    if action == "finished_then_resumed":
        player.finished()
        action = "resumed"
    with pytest.raises(InvalidTransition):
        getattr(player, action)(*([entry()] if action == "started" else []))


def test_session_and_entry_matching():
    player = GuildPlayer(GUILD_A, max_queue=5, volume=50)
    assert not player.matches("abcdef12")
    session = player.connected_to(VOICE_A)
    e = entry()
    player.started(e)
    assert player.matches(session) and player.matches(session, e.entry_id)
    assert not player.matches(session, e.entry_id + 1)  # le titre affiché n'est plus en cours
    assert not player.matches("00000000") and not player.matches("")


def test_players_are_isolated_per_guild():
    manager = PlayerManager(max_queue=5, default_volume=40)
    a = manager.get_or_create(GUILD_A)
    b = manager.get_or_create(GUILD_B, volume=80)
    assert a is not b and a.queue is not b.queue and a.lock is not b.lock
    a.queue.add(entry())
    assert len(b.queue) == 0 and (a.volume, b.volume) == (40, 80)
    assert manager.get_or_create(GUILD_A) is a and manager.get(3) is None and len(manager) == 2


# --- Autorisations ---------------------------------------------------------------------------


def connected_player(requester: int = 1) -> GuildPlayer:
    player = GuildPlayer(GUILD_A, max_queue=5, volume=50)
    player.connected_to(VOICE_A)
    player.started(entry(requester=requester))
    return player


def test_must_be_in_voice_and_in_the_bot_channel():
    policy = MusicPolicy()
    with pytest.raises(NotInVoice):
        policy.require_same_channel(None, listener(voice=None))
    policy.require_same_channel(None, listener(voice=VOICE_A2))  # bot absent : n'importe quel salon
    with pytest.raises(WrongVoiceChannel):
        policy.require_same_channel(connected_player(), listener(voice=VOICE_A2))


def test_without_dj_role_everyone_with_the_bot_controls():
    policy = MusicPolicy(dj_role_configured=False)
    player = connected_player(requester=1)
    policy.require_dj(player, listener(2), "changer le volume")
    policy.require_skip(player, listener(2), player.current)


def test_with_dj_role_restricted_actions():
    policy = MusicPolicy(dj_role_configured=True)
    player = connected_player(requester=1)
    with pytest.raises(ControlDenied):
        policy.require_dj(player, listener(2), "changer le volume")
    with pytest.raises(ControlDenied):
        policy.require_skip(player, listener(2), player.current)
    policy.require_skip(player, listener(1), player.current)  # son propre titre
    policy.require_dj(player, listener(2, dj=True), "changer le volume")
    policy.require_dj(player, listener(2, admin=True), "changer le volume")
    policy.require_dj(player, listener(2, humans=1), "changer le volume")  # seule avec le bot


def test_admin_may_stop_from_anywhere_but_others_must_join():
    policy = MusicPolicy(dj_role_configured=True)
    player = connected_player()
    policy.require_dj(player, listener(9, voice=None, admin=True), "arrêter", admin_anywhere=True)
    with pytest.raises(NotInVoice):
        policy.require_dj(player, listener(9, voice=None, dj=True), "arrêter", admin_anywhere=True)
    with pytest.raises(WrongVoiceChannel):
        policy.require_dj(player, listener(9, voice=VOICE_A2, dj=True), "changer le volume")


def test_alone_rule_only_counts_in_the_bot_channel():
    policy = MusicPolicy(dj_role_configured=True)
    player = connected_player()
    assert not policy.is_dj(player, listener(2, voice=VOICE_A2, humans=1))
    assert policy.is_dj(player, listener(2, voice=VOICE_A, humans=1))
