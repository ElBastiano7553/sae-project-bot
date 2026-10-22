"""Statistiques et classement des contributions, calculés uniquement à partir des événements reçus.

Source des données : les webhooks GitLab reçus par le bot depuis leur activation
(colonne ``source = 'webhook'``). Le bot ne connaît rien d'antérieur et rien de ce
que GitLab n'a pas envoyé : chaque résultat indique sa période et sa couverture.

Choix de mesure (documentés aussi dans docs/history-and-stats.md) :

* un pipeline compte une fois, avec son **dernier** statut final reçu ;
* une merge request fusionnée est attribuée à son **auteur** (identifiant GitLab
  ``author_id``), pas à la personne qui a cliqué sur « Merge » ;
* une approbation compte une fois par membre et par MR, jamais pour sa propre MR,
  et plus du tout si elle a été retirée ensuite ;
* les « jours actifs » comptent les jours distincts avec au moins un push : pousser
  vingt petits commits le même jour ne vaut pas plus qu'un seul gros push ;
* le nombre de commits est affiché à titre indicatif mais **n'entre pas** dans le
  classement : ce n'est pas une mesure de la qualité ni de la quantité de travail.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from src.database.repositories import EventRecord
from src.gitlab.rules import FINAL_PIPELINE_STATUSES

# Périodes proposées par les commandes : libellé → nombre de jours (None = tout l'historique conservé).
PERIODS: dict[str, int | None] = {"24h": 1, "7j": 7, "30j": 30, "90j": 90, "tout": None}
PERIOD_LABELS = {
    "24h": "dernières 24 heures",
    "7j": "7 derniers jours",
    "30j": "30 derniers jours",
    "90j": "90 derniers jours",
    "tout": "tout l'historique conservé",
}
PUSH_ACTIONS = {"push", "branch_created"}
APPROVAL_ACTIONS = {"approved", "approval"}
UNAPPROVAL_ACTIONS = {"unapproved", "unapproval"}
# En dessous, le bot affiche les données sans classer personne.
MIN_EVENTS_FOR_RANKING = 5
MIN_CONTRIBUTORS_FOR_RANKING = 2

SQLITE_FORMAT = "%Y-%m-%d %H:%M:%S"


def period_start(period: str, now: dt.datetime) -> str | None:
    """Début de la période au format SQLite (UTC), ou ``None`` pour tout l'historique."""
    if period not in PERIODS:
        raise ValueError(f"période inconnue : {period}")
    days = PERIODS[period]
    if days is None:
        return None
    return (now.astimezone(dt.UTC) - dt.timedelta(days=days)).strftime(SQLITE_FORMAT)


def previous_period(period: str, now: dt.datetime) -> tuple[str, str] | None:
    """Période précédente de même durée (début, fin), pour mesurer l'évolution."""
    days = PERIODS[period]
    if days is None:
        return None
    end = now.astimezone(dt.UTC) - dt.timedelta(days=days)
    return (end - dt.timedelta(days=days)).strftime(SQLITE_FORMAT), end.strftime(SQLITE_FORMAT)


@dataclass(frozen=True)
class ActivityStats:
    events: int = 0
    pushes: int = 0
    commits: int = 0
    mr_opened: int = 0
    mr_merged: int = 0
    pipelines: Counter = field(default_factory=Counter)  # statut final → nombre de pipelines
    deployments: Counter = field(default_factory=Counter)  # statut → nombre de déploiements


def compute_stats(records: Iterable[EventRecord], start: str | None = None, end: str | None = None) -> ActivityStats:
    """Statistiques des événements dont la date est dans [start, end[ (bornes facultatives)."""
    selected = [r for r in records if _in_range(r, start, end)]
    pushes = [r for r in selected if r.event_type == "push" and r.action in PUSH_ACTIONS]
    opened = {r.subject_id for r in selected if r.event_type == "merge_request" and r.action == "open"}
    merged = {r.subject_id for r in selected if r.event_type == "merge_request" and r.action == "merge"}
    return ActivityStats(
        events=len(selected),
        pushes=len(pushes),
        commits=sum(r.commit_count or 0 for r in pushes),
        mr_opened=len(opened),
        mr_merged=len(merged),
        pipelines=Counter(_latest_status(selected, "pipeline", FINAL_PIPELINE_STATUSES).values()),
        deployments=Counter(_latest_status(selected, "deployment", None).values()),
    )


def weekly_activity(records: Iterable[EventRecord], now: dt.datetime, weeks: int = 4) -> list[tuple[str, int, int]]:
    """Par semaine glissante (la plus ancienne d'abord) : (libellé, commits, MR fusionnées)."""
    now = now.astimezone(dt.UTC)
    records = list(records)
    result = []
    for index in range(weeks - 1, -1, -1):
        end = now - dt.timedelta(days=7 * index)
        start = end - dt.timedelta(days=7)
        stats = compute_stats(records, start.strftime(SQLITE_FORMAT), end.strftime(SQLITE_FORMAT))
        result.append((f"{start:%d/%m}–{end:%d/%m}", stats.commits, stats.mr_merged))
    return result


def _in_range(record: EventRecord, start: str | None, end: str | None) -> bool:
    moment = record.event_at or record.received_at
    return (start is None or moment >= start) and (end is None or moment < end)


def _latest_status(records: list[EventRecord], kind: str, allowed: Iterable[str] | None) -> dict[int, str]:
    allowed = set(allowed) if allowed is not None else None
    latest: dict[int, str] = {}
    for record in sorted((r for r in records if r.event_type == kind), key=lambda r: r.event_at or r.received_at):
        if record.subject_id is None or not record.action:
            continue
        if allowed is None or record.action in allowed:
            latest[record.subject_id] = record.action
    return latest


# --- Contributions ------------------------------------------------------------------


@dataclass
class Contribution:
    user_id: int
    name: str
    merged_mrs: int = 0
    approvals: int = 0
    active_days: int = 0
    commits: int = 0
    mrs_opened: int = 0

    @property
    def ranking_key(self) -> tuple[int, int, int]:
        return (self.merged_mrs, self.approvals, self.active_days)


@dataclass(frozen=True)
class Leaderboard:
    contributions: list[Contribution]
    ranked: bool
    reason: str = ""  # pourquoi aucun classement n'est proposé
    unattributed_merges: int = 0  # MR fusionnées dont GitLab n'a pas indiqué l'auteur
    events: int = 0

    def ranks(self) -> list[tuple[int, Contribution]]:
        """Rang de chaque membre ; deux membres aux mêmes valeurs ont le même rang."""
        result, previous, rank = [], None, 0
        for position, item in enumerate(self.contributions, start=1):
            if item.ranking_key != previous:
                rank, previous = position, item.ranking_key
            result.append((rank, item))
        return result


def compute_leaderboard(records: Iterable[EventRecord], names: dict[int, str]) -> Leaderboard:
    records = list(records)
    people: dict[int, Contribution] = {}

    def person(user_id: int) -> Contribution:
        if user_id not in people:
            people[user_id] = Contribution(user_id, names.get(user_id) or f"membre GitLab n°{user_id}")
        return people[user_id]

    merged_by_author: dict[int, set[int]] = {}
    unattributed: set[int] = set()
    approvals: dict[int, set[int]] = {}
    opened: dict[int, set[int]] = {}
    days: dict[int, set[str]] = {}
    for r in sorted(records, key=lambda r: r.event_at or r.received_at):
        if r.event_type == "merge_request" and r.subject_id is not None:
            if r.action == "merge":
                if r.subject_author_id is None:
                    unattributed.add(r.subject_id)
                else:
                    merged_by_author.setdefault(r.subject_author_id, set()).add(r.subject_id)
            elif r.action in APPROVAL_ACTIONS and r.user_id is not None and r.user_id != r.subject_author_id:
                approvals.setdefault(r.user_id, set()).add(r.subject_id)
            elif r.action in UNAPPROVAL_ACTIONS and r.user_id is not None:
                approvals.get(r.user_id, set()).discard(r.subject_id)
            elif r.action == "open" and r.user_id is not None:
                opened.setdefault(r.user_id, set()).add(r.subject_id)
        elif r.event_type == "push" and r.action in PUSH_ACTIONS and r.user_id is not None:
            person(r.user_id).commits += r.commit_count or 0
            days.setdefault(r.user_id, set()).add((r.event_at or r.received_at)[:10])
    for user_id, mrs in merged_by_author.items():
        person(user_id).merged_mrs = len(mrs)
    for user_id, mrs in approvals.items():
        person(user_id).approvals = len(mrs)
    for user_id, mrs in opened.items():
        person(user_id).mrs_opened = len(mrs)
    for user_id, active in days.items():
        person(user_id).active_days = len(active)

    contributions = sorted(people.values(), key=lambda c: (tuple(-v for v in c.ranking_key), c.name.casefold()))
    active = [c for c in contributions if any(c.ranking_key)]
    reason = ""
    if len(records) < MIN_EVENTS_FOR_RANKING:
        reason = f"moins de {MIN_EVENTS_FOR_RANKING} événements GitLab enregistrés sur la période"
    elif len(active) < MIN_CONTRIBUTORS_FOR_RANKING:
        reason = f"moins de {MIN_CONTRIBUTORS_FOR_RANKING} membres avec une activité mesurée sur la période"
    if reason:
        contributions = sorted(people.values(), key=lambda c: c.name.casefold())
    return Leaderboard(contributions, ranked=not reason, reason=reason, unattributed_merges=len(unattributed), events=len(records))
