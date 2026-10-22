"""Statistiques et classement : calculs faits uniquement sur les événements fournis."""

import datetime as dt

import pytest

from src.database.repositories import EventRecord
from src.services.history import (
    MIN_EVENTS_FOR_RANKING,
    compute_leaderboard,
    compute_stats,
    period_start,
    previous_period,
    weekly_activity,
)

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


def event(kind, action, at="2026-10-21 10:00:00", **extra) -> EventRecord:
    return EventRecord(
        event_type=kind,
        status="sent",
        action=action,
        ref=extra.pop("ref", "main"),
        title=extra.pop("title", f"{kind} {action}"),
        url=None,
        author=extra.pop("author", None),
        occurred_at=at,
        received_at=at,
        event_at=at,
        **extra,
    )


def push(user, at="2026-10-21 10:00:00", commits=1):
    return event("push", "push", at, user_id=user, commit_count=commits)


def mr(action, mr_id, user, author, at="2026-10-21 10:00:00"):
    return event("merge_request", action, at, subject_id=mr_id, user_id=user, subject_author_id=author)


def pipeline(pipeline_id, status, at="2026-10-21 10:00:00"):
    return event("pipeline", status, at, subject_id=pipeline_id)


def test_period_start_and_previous_period():
    assert period_start("7j", NOW) == "2026-10-15 12:00:00"
    assert period_start("tout", NOW) is None
    assert previous_period("24h", NOW) == ("2026-10-20 12:00:00", "2026-10-21 12:00:00")
    assert previous_period("tout", NOW) is None
    with pytest.raises(ValueError):
        period_start("1an", NOW)


def test_stats_count_each_pipeline_once_with_its_last_final_status():
    records = [
        pipeline(1, "failed", "2026-10-21 09:00:00"),
        pipeline(1, "success", "2026-10-21 09:30:00"),  # relancé puis réussi
        pipeline(2, "running", "2026-10-21 10:00:00"),  # pas un statut final
        pipeline(3, "failed", "2026-10-21 11:00:00"),
    ]
    stats = compute_stats(records)
    assert stats.pipelines == {"success": 1, "failed": 1}


def test_stats_merge_requests_are_distinct_and_period_is_applied():
    records = [
        mr("open", 10, 1, 1, "2026-10-10 10:00:00"),  # hors période
        mr("open", 11, 1, 1),
        mr("update", 11, 1, 1),
        mr("merge", 11, 2, 1),
        mr("merge", 11, 2, 1),  # même MR reçue deux fois
        push(1, commits=3),
        push(2, commits=2),
    ]
    stats = compute_stats(records, start="2026-10-15 00:00:00")
    assert (stats.mr_opened, stats.mr_merged) == (1, 1)
    assert (stats.pushes, stats.commits) == (2, 5)
    assert stats.events == 6


def test_stats_end_bound_is_exclusive():
    records = [push(1, "2026-10-21 10:00:00"), push(1, "2026-10-22 10:00:00")]
    assert compute_stats(records, end="2026-10-22 10:00:00").pushes == 1


def test_weekly_activity_oldest_first():
    records = [push(1, "2026-10-21 10:00:00", commits=4), mr("merge", 5, 1, 1, "2026-10-01 10:00:00")]
    weeks = weekly_activity(records, NOW, weeks=4)
    assert len(weeks) == 4
    assert weeks[-1][1:] == (4, 0)
    assert weeks[0][1:] == (0, 1)


def test_no_ranking_with_too_few_events():
    board = compute_leaderboard([push(1), push(2)], {1: "Alice", 2: "Bob"})
    assert not board.ranked
    assert str(MIN_EVENTS_FOR_RANKING) in board.reason
    assert [c.name for c in board.contributions] == ["Alice", "Bob"]  # ordre alphabétique, sans rang


def test_no_ranking_with_a_single_active_member():
    board = compute_leaderboard([push(1, f"2026-10-{d:02d} 10:00:00") for d in range(1, 8)], {1: "Alice"})
    assert not board.ranked
    assert "membres" in board.reason


def test_ranking_uses_merged_mrs_then_approvals_then_active_days_never_commits():
    records = [
        # Alice : 1 MR fusionnée (fusionnée par Bob : elle reste attribuée à son autrice).
        mr("open", 1, 1, 1),
        mr("merge", 1, 2, 1),
        # Bob : 0 MR fusionnée, 1 approbation de la MR d'Alice, énormément de commits.
        mr("approved", 1, 2, 1),
        mr("approved", 1, 2, 1),  # la même approbation reçue deux fois ne compte qu'une fois
        push(2, commits=500),
        # Chloé : approuve sa propre MR (ne compte pas), 2 jours actifs.
        mr("approved", 2, 3, 3),
        push(3, "2026-10-20 10:00:00"),
        push(3, "2026-10-21 10:00:00"),
    ]
    board = compute_leaderboard(records, {1: "Alice", 2: "Bob", 3: "Chloé"})
    assert board.ranked
    by_name = {c.name: c for c in board.contributions}
    assert by_name["Alice"].merged_mrs == 1 and by_name["Bob"].merged_mrs == 0
    assert by_name["Bob"].approvals == 1
    assert by_name["Chloé"].approvals == 0
    assert by_name["Bob"].commits == 500
    assert [c.name for c in board.contributions] == ["Alice", "Bob", "Chloé"]


def test_equal_members_share_the_same_rank():
    records = [push(1), push(2), push(3, "2026-10-20 10:00:00"), push(3), push(1, "2026-10-21 11:00:00")]
    board = compute_leaderboard(records, {1: "Alice", 2: "Bob", 3: "Chloé"})
    assert board.ranked
    ranks = {c.name: rank for rank, c in board.ranks()}
    assert ranks == {"Chloé": 1, "Alice": 2, "Bob": 2}


def test_unknown_author_is_counted_but_not_attributed():
    records = [mr("merge", 9, 2, None), push(1), push(2), push(1, "2026-10-20 10:00:00"), push(2, "2026-10-19 10:00:00")]
    board = compute_leaderboard(records, {})
    assert board.unattributed_merges == 1
    assert all(c.merged_mrs == 0 for c in board.contributions)
    assert {c.name for c in board.contributions} == {"membre GitLab n°1", "membre GitLab n°2"}


def test_withdrawn_approval_no_longer_counts():
    records = [
        mr("approved", 1, 2, 1, "2026-10-21 09:00:00"),
        mr("unapproved", 1, 2, 1, "2026-10-21 09:30:00"),
        mr("approved", 3, 2, 1, "2026-10-21 10:00:00"),
    ]
    board = compute_leaderboard(records, {1: "Alice", 2: "Bob"})
    assert next(c for c in board.contributions if c.name == "Bob").approvals == 1
