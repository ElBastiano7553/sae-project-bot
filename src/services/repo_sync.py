"""Synchronisation du suivi avec le dépôt GitHub (1.15.0, docs/synchronisation-github.md).

``RepoSyncService.sync`` relit le dépôt (``src/github/repo_sync.py``), enregistre les commits de la
branche principale et les pull requests, puis relie ce code au classeur de suivi grâce aux
identifiants cités (« FEAT-002 » dans un message de commit, un titre ou une branche de PR) :

* un élément VALIDÉ sur lequel du code arrive → proposition de passer à EN COURS ;
* un élément VALIDÉ ou EN COURS annoncé terminé (« termine FEAT-002 », « closes GP-003 ») dans un
  commit de la branche principale ou une PR **fusionnée** → proposition de passer à TERMINÉ.

Ce ne sont que des **propositions** (``TrackerService.propose_update``) : un administrateur les valide
ou les refuse avec ``/suivi valider`` / ``/suivi refuser``. Rien n'est proposé pour un élément non
validé par l'équipe (PROPOSÉ, À DISCUTER…) : il est seulement signalé. Une même proposition n'est
jamais faite deux fois (commit déjà lu, ou proposition identique déjà en attente). Rien n'est écrit
sur GitHub.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field

from src.database.connection import Database
from src.database.repo_sync import RepoSyncRepository, SyncItem
from src.database.repositories import KeyValueRepository
from src.database.tracker import TrackerChange
from src.github.repo_sync import GitHubRepoClient, GitHubSyncError, Parsed, commit_item, pull_item
from src.services.meetings import Actor
from src.services.tracker import TrackerError, TrackerService
from src.tracker.checks import KNOWN_PREFIXES
from src.tracker.workbook import norm

log = logging.getLogger(__name__)

# Onglets dont les lignes ont un avancement (statut VALIDÉ → EN COURS → TERMINÉ).
PROGRESS_SHEETS = ("FEATURES", "GAMEPLAY", "UI_UX", "ART_DIRECTION")
VALIDATED, IN_PROGRESS, DONE = "VALIDÉ", "EN COURS", "TERMINÉ"
CLOSED_STATUSES = ("TERMINÉ", "ABANDONNÉ")
FIRST_SYNC_DAYS = 30
SYNC_ACTOR_NAME = "Samae (synchronisation GitHub)"


def cursor_key(repository: str) -> str:
    return f"github_sync:{repository}:since"


def last_run_key(repository: str) -> str:
    return f"github_sync:{repository}:last"


@dataclass
class SyncResult:
    repository: str
    branch: str = ""
    commits_read: int = 0
    new_commits: int = 0
    pulls_read: int = 0
    new_pulls: int = 0
    newly_merged: int = 0
    touched: dict[str, list[str]] = field(default_factory=dict)  # élément du suivi → commits / PR qui le citent
    proposals: list[TrackerChange] = field(default_factory=list)
    unvalidated: list[str] = field(default_factory=list)  # éléments non validés sur lesquels du code arrive
    unknown: list[str] = field(default_factory=list)  # identifiants cités absents du suivi
    tracker_loaded: bool = True
    at: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "at": self.at, "branch": self.branch, "new_commits": self.new_commits, "new_pulls": self.new_pulls,
            "newly_merged": self.newly_merged, "proposals": [c.id for c in self.proposals], "touched": sorted(self.touched),
        }, ensure_ascii=False)


class RepoSyncService:
    def __init__(
        self,
        db: Database,
        tracker: TrackerService,
        repository: str,
        *,
        token: str = "",
        api_url: str | None = None,
        guild_id=lambda: None,
        now=lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.repository = repository
        self.items = RepoSyncRepository(db)
        self.state = KeyValueRepository(db)
        self.tracker = tracker
        self._client = GitHubRepoClient(repository, token=token, **({"api_url": api_url} if api_url else {})) if repository else None
        self._guild_id = guild_id
        self._now = now
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def last_run(self) -> dict | None:
        raw = self.state.get(last_run_key(self.repository)) if self.repository else None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def mentioning(self, key: str) -> list[SyncItem]:
        return self.items.mentioning(self.repository, key) if self.repository else []

    def _actor(self) -> Actor:
        return Actor(0, SYNC_ACTOR_NAME, False, self._guild_id())

    def _tracker_rows(self) -> dict[str, tuple[str, str]]:
        """Identifiant → (onglet, statut actuel), pour les onglets qui ont un avancement."""
        rows: dict[str, tuple[str, str]] = {}
        for sheet in PROGRESS_SHEETS:
            for row in self.tracker.rows(sheet):
                rows[row.key] = (sheet, row.status)
        return rows

    def _known_ids(self) -> set[str]:
        keys: set[str] = set()
        for sheet in ("FEATURES", "IDEAS", "DECISIONS", "GAMEPLAY", "UI_UX", "ART_DIRECTION", "OPEN_QUESTIONS"):
            keys.update(row.key for row in self.tracker.rows(sheet))
        return keys

    def _status_value(self, sheet: str, wanted: str) -> str | None:
        """Statut ``wanted`` tel qu'il s'écrit dans le classeur importé (None s'il n'existe pas)."""
        allowed = self.tracker.allowed_statuses(sheet) or [wanted]
        return next((s for s in allowed if norm(s) == norm(wanted)), None)

    async def sync(self) -> SyncResult:
        """Relit le dépôt et propose les changements de statut. Lève ``GitHubSyncError`` si GitHub ne répond pas."""
        if self._client is None:
            raise GitHubSyncError("Synchronisation non configurée : indiquez GITHUB_SYNC_REPOSITORY=propriétaire/dépôt dans .env.")
        if self._lock.locked():
            raise GitHubSyncError("Une synchronisation est déjà en cours.")
        async with self._lock:
            since = self.state.get(cursor_key(self.repository))
            if since is None:
                since = (self._now() - dt.timedelta(days=FIRST_SYNC_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            branch, commits, pulls = await self._client.fetch(since=since)
            return self._record(branch, commits, pulls, since)

    def _record(self, branch: str, commits: list[dict], pulls: list[dict], since: str) -> SyncResult:
        result = SyncResult(self.repository, branch, at=self._now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        rows = self._tracker_rows()
        result.tracker_loaded = self.tracker.info() is not None
        known = self._known_ids()
        prefixes = KNOWN_PREFIXES | {key.split("-")[0] for key in known if "-" in key}
        newest = since
        work: list[tuple[Parsed, bool]] = []  # (élément lu, peut-il annoncer une fin ?)
        for data in commits:
            parsed = commit_item(self.repository, data, prefixes)
            if parsed is None:
                continue
            result.commits_read += 1
            is_new, _ = self.items.record(parsed.item)
            if parsed.item.occurred_at and parsed.item.occurred_at > newest:
                newest = parsed.item.occurred_at
            if is_new:
                result.new_commits += 1
                work.append((parsed, True))  # commit de la branche principale : le travail y est intégré
        for data in pulls:
            parsed = pull_item(self.repository, data, prefixes)
            if parsed is None:
                continue
            result.pulls_read += 1
            is_new, old_state = self.items.record(parsed.item)
            merged_now = parsed.item.state == "merged" and old_state != "merged"
            if is_new:
                result.new_pulls += 1
            if merged_now and not is_new:
                result.newly_merged += 1
            if is_new or merged_now:
                work.append((parsed, parsed.item.state == "merged"))
        pending = {(c.sheet, c.row_key, json.dumps(c.payload, sort_keys=True)) for c in self.tracker.pending()}
        for parsed, can_complete in work:
            item = parsed.item
            for ref in item.refs:
                result.touched.setdefault(ref, []).append(f"{'PR' if item.kind == 'pull' else 'commit'} {item.label}")
                if ref not in known:
                    if ref not in result.unknown:
                        result.unknown.append(ref)
                    continue
                if ref not in rows:
                    continue  # idée, décision, question : pas d'avancement
                sheet, status = rows[ref]
                change = self._propose(sheet, ref, status, item, ref in parsed.completes and can_complete, pending, result)
                if change is not None:
                    rows[ref] = (sheet, change.payload.get(next(iter(change.payload)), status))
        self.state.set(cursor_key(self.repository), newest)
        self.state.set(last_run_key(self.repository), result.to_json())
        log.info("Synchronisation GitHub %s : %d commit(s) et %d PR nouveaux, %d proposition(s)",
                 self.repository, result.new_commits, result.new_pulls, len(result.proposals))
        return result

    def _propose(self, sheet: str, key: str, status: str, item: SyncItem, completes: bool, pending: set, result: SyncResult):
        current = norm(status)
        if current in (norm(s) for s in CLOSED_STATUSES):
            return None
        if current not in (norm(VALIDATED), norm(IN_PROGRESS)):
            if key not in result.unvalidated:
                result.unvalidated.append(key)
            return None
        target = DONE if completes else (IN_PROGRESS if current == norm(VALIDATED) else None)
        if target is None:
            return None
        value = self._status_value(sheet, target)
        info = self.tracker.sheet_info(sheet)
        if value is None or info is None or not info.status_column:
            return None
        payload_key = (sheet, key, json.dumps({info.status_column: value}, sort_keys=True))
        if payload_key in pending:
            return None
        what = "PR fusionnée" if item.kind == "pull" else "commit"
        reason = f"Synchronisation GitHub : {what} {item.label} « {item.title} »" + (f" ({item.url})" if item.url else "")
        try:
            change = self.tracker.propose_update(sheet, key, self._actor(), info.status_column, value, reason)
        except TrackerError as exc:
            log.info("Proposition non faite pour %s : %s", key, exc)
            return None
        pending.add(payload_key)
        result.proposals.append(change)
        return change
