"""Synchronisation du suivi avec le dépôt GitHub (1.15.0) : faux GitHub local, aucun réseau."""

from __future__ import annotations

import datetime as dt

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.config import ConfigError, Settings
from src.database.connection import MIGRATIONS
from src.github.repo_sync import GitHubRepoClient, GitHubSyncError, commit_item, completed_ids, pull_item
from src.services.meetings import Actor
from src.services.repo_sync import RepoSyncService, cursor_key
from src.services.tracker import TrackerService
from tests import lunasteh_fixture

REPO = "ElBastiano7553/game-project"
ADMIN = Actor(1, "Romain", True, 42)
NOW = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.UTC)
PREFIXES = {"FEAT", "GP", "IDEA", "Q", "DEC", "ART"}


def sha(n: int) -> str:
    return f"{n:040x}"


def commit(n: int, message: str, date: str = "2026-11-27T10:00:00Z", login: str = "alice") -> dict:
    return {
        "sha": sha(n), "html_url": f"https://github.com/{REPO}/commit/{sha(n)}", "author": {"login": login},
        "commit": {"message": message, "author": {"name": "Alice", "date": date}, "committer": {"date": date}},
    }


def pull(number: int, title: str, *, body: str = "", state: str = "open", merged: bool = False, branch: str = "dev") -> dict:
    return {
        "number": number, "title": title, "body": body, "state": state, "merged_at": "2026-11-27T12:00:00Z" if merged else None,
        "updated_at": "2026-11-27T12:00:00Z", "html_url": f"https://github.com/{REPO}/pull/{number}", "user": {"login": "bob"},
        "head": {"ref": branch},
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.commits: list[dict] = []
        self.pulls: list[dict] = []
        self.status = 200
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict, str | None]] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(f"/repos/{REPO}", self.repo)
        app.router.add_get(f"/repos/{REPO}/commits", self.list_commits)
        app.router.add_get(f"/repos/{REPO}/pulls", self.list_pulls)
        return app

    def _note(self, request) -> web.Response | None:
        self.requests.append((request.path, dict(request.query), request.headers.get("Authorization")))
        if self.status != 200:
            return web.json_response({"message": "erreur"}, status=self.status, headers=self.headers)
        return None

    async def repo(self, request):
        return self._note(request) or web.json_response({"default_branch": "main"})

    async def list_commits(self, request):
        return self._note(request) or web.json_response(self.commits)

    async def list_pulls(self, request):
        return self._note(request) or web.json_response(self.pulls)


@pytest.fixture
async def github():
    fake = FakeGitHub()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
def tracker(db, tmp_path):
    service = TrackerService(db, tmp_path / "tracker", backup_dir=tmp_path / "backups")
    service.apply_import(lunasteh_fixture.xlsx(), "lunasteh.xlsx", ADMIN)
    return service


def make_sync(db, tracker, github, **kwargs):
    return RepoSyncService(db, tracker, REPO, api_url=github.url, guild_id=lambda: 42, now=lambda: NOW, **kwargs)


def statuses(changes):
    return {(c.row_key, next(iter(c.payload.values()))) for c in changes}


# --- Analyse des commits et des PR --------------------------------------------------------------------


def test_completion_words():
    assert completed_ids("Termine FEAT-001 et avance FEAT-003", ["FEAT-001", "FEAT-003"]) == ("FEAT-001",)
    assert completed_ids("closes: GP-001", ["GP-001"]) == ("GP-001",)
    assert completed_ids("Travail sur FEAT-001 (pas terminé)", ["FEAT-001"]) == ()


def test_items_are_cleaned_and_refs_extracted():
    parsed = commit_item(REPO, commit(1, "Saut @everyone FEAT-001\n\nDétails : voir Q-001"), PREFIXES)
    assert parsed.item.title.startswith("Saut @​everyone") and parsed.item.refs == ("FEAT-001", "Q-001")
    assert commit_item(REPO, {"sha": "pas-un-sha"}, PREFIXES) is None
    pr = pull_item(REPO, pull(7, "Inventaire", branch="feat-003-arbre", merged=True, state="closed"), PREFIXES)
    assert pr.item.state == "merged" and pr.item.refs == ("FEAT-003",) and pr.item.label == "#7"
    assert pull_item(REPO, {"number": True}, PREFIXES) is None


def test_invalid_repository_is_refused():
    with pytest.raises(GitHubSyncError):
        GitHubRepoClient("pas un dépôt")


# --- Synchronisation ----------------------------------------------------------------------------------


async def test_first_sync_proposes_statuses_and_never_applies_them(db, tracker, github):
    github.commits = [
        commit(1, "Saut : premières animations (FEAT-001)"),
        commit(2, "Termine FEAT-003, l'arbre est jouable"),
        commit(3, "Prototype FEAT-002 et FEAT-099"),
    ]
    github.pulls = [pull(5, "GP-001 : début de partie"), pull(6, "Brouillon", state="closed")]
    sync = make_sync(db, tracker, github)
    result = await sync.sync()
    assert (result.branch, result.new_commits, result.new_pulls) == ("main", 3, 2)
    assert statuses(result.proposals) == {("FEAT-001", "EN COURS"), ("FEAT-003", "TERMINÉ"), ("GP-001", "EN COURS")}
    assert all(c.status == "pending" and c.proposer == "Samae (synchronisation GitHub)" for c in result.proposals)
    assert result.unvalidated == ["FEAT-002"] and result.unknown == ["FEAT-099"]
    # Rien n'est appliqué : le classeur garde ses statuts tant qu'un administrateur n'a pas validé.
    assert {r.key: r.status for r in tracker.rows("FEATURES")} == {"FEAT-001": "VALIDÉ", "FEAT-002": "PROPOSÉ", "FEAT-003": "EN COURS"}
    # Premier passage : 30 jours d'historique ; la branche principale est lue.
    path, query, auth = next(r for r in github.requests if r[0].endswith("/commits"))
    assert query["since"] == "2026-10-29T12:00:00Z" and query["sha"] == "main" and auth is None
    assert db.conn.execute("SELECT value FROM kv_store WHERE key = ?", (cursor_key(REPO),)).fetchone()[0] == "2026-11-27T10:00:00Z"
    assert [i.label for i in sync.mentioning("FEAT-001")] == [sha(1)[:7]]


async def test_a_second_sync_proposes_nothing_twice(db, tracker, github):
    github.commits = [commit(1, "FEAT-001 : animations")]
    sync = make_sync(db, tracker, github)
    first = await sync.sync()
    tracker.reject(first.proposals[0].id, ADMIN)
    again = await sync.sync()
    assert (again.new_commits, again.proposals) == (0, [])
    # Un nouveau commit qui cite encore FEAT-001 : nouvelle proposition (la précédente a été refusée).
    github.commits.append(commit(2, "FEAT-001 : collisions", date="2026-11-28T09:00:00Z"))
    third = await sync.sync()
    assert statuses(third.proposals) == {("FEAT-001", "EN COURS")}
    # Encore un commit, alors qu'une proposition identique attend : pas de doublon.
    github.commits.append(commit(3, "FEAT-001 : sons", date="2026-11-28T10:00:00Z"))
    assert (await sync.sync()).proposals == []


async def test_a_pull_request_merged_later_proposes_done(db, tracker, github):
    github.pulls = [pull(9, "Arbre de compétences", body="Closes FEAT-003")]
    sync = make_sync(db, tracker, github)
    opened = await sync.sync()
    assert opened.proposals == []  # FEAT-003 est déjà EN COURS ; une PR ouverte ne termine rien
    github.pulls = [pull(9, "Arbre de compétences", body="Closes FEAT-003", state="closed", merged=True)]
    merged = await sync.sync()
    assert merged.newly_merged == 1 and statuses(merged.proposals) == {("FEAT-003", "TERMINÉ")}


async def test_sync_without_imported_tracker_still_records_the_code(db, tmp_path, github):
    empty = TrackerService(db, tmp_path / "vide")
    github.commits = [commit(1, "FEAT-001 : saut")]
    result = await make_sync(db, empty, github).sync()
    assert not result.tracker_loaded and result.new_commits == 1 and result.proposals == []


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [(403, {"X-RateLimit-Remaining": "0"}, "Limite de requêtes"), (401, {}, "refuse l'accès"), (404, {}, "introuvable"), (502, {}, "HTTP 502")],
)
async def test_github_errors_are_explained(db, tracker, github, status, headers, message):
    github.status, github.headers = status, headers
    with pytest.raises(GitHubSyncError, match=message):
        await make_sync(db, tracker, github, token="ghp_secret").sync()
    assert github.requests[0][2] == "Bearer ghp_secret"


async def test_disabled_without_repository(db, tracker):
    sync = RepoSyncService(db, tracker, "")
    assert not sync.enabled and sync.mentioning("FEAT-001") == [] and sync.last_run() is None
    with pytest.raises(GitHubSyncError, match="GITHUB_SYNC_REPOSITORY"):
        await sync.sync()


def test_migration_v16_only_creates():
    script = MIGRATIONS[15].upper()
    assert "CREATE TABLE REPO_SYNC_ITEMS" in script
    for forbidden in ("DROP ", "DELETE ", "UPDATE ", "ALTER "):
        assert forbidden not in script


def test_settings(tmp_path):
    base = {"DISCORD_BOT_TOKEN": "x" * 60}
    settings = Settings.from_env({**base, "GITHUB_SYNC_REPOSITORY": REPO, "GITHUB_SYNC_TOKEN": "ghp_x", "GITHUB_SYNC_INTERVAL_MINUTES": "60"},
                                 project_root=tmp_path)
    assert (settings.github_sync_repository, settings.github_sync_interval_minutes) == (REPO, 60)
    assert "ghp_x" not in repr(settings)
    for bad in ({"GITHUB_SYNC_REPOSITORY": "pas/un/dépôt"}, {"GITHUB_SYNC_INTERVAL_MINUTES": "5"}):
        with pytest.raises(ConfigError):
            Settings.from_env({**base, **bad}, project_root=tmp_path)
