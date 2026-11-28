"""Lecture du dépôt GitHub du jeu par l'API REST (lecture seule), pour la synchronisation avec le suivi.

Les webhooks annoncent les nouveautés en direct, mais GitHub ne renvoie jamais un webhook perdu
(bot arrêté, VPS injoignable). La synchronisation relit donc régulièrement le dépôt :

* ``GitHubRepoClient`` : branche principale, derniers commits de cette branche, dernières pull
  requests (``GET /repos/{dépôt}``, ``/commits``, ``/pulls``). Un jeton est facultatif pour un dépôt
  public (60 requêtes par heure sans jeton) ; il faut un jeton **en lecture seule** pour un dépôt privé ;
* ``commit_item`` / ``pull_item`` : transforment la réponse JSON en ``SyncItem`` (texte nettoyé,
  éléments du suivi cités).

Ce module ne dépend pas de Discord et n'écrit jamais rien sur GitHub.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import aiohttp

from src.database.repo_sync import SyncItem
from src.gitlab.security import clean_text, first_line, safe_url
from src.providers.github_app import API_VERSION, GITHUB_API_URL, is_rate_limited
from src.tracker.checks import mentioned_ids

MAX_COMMITS = 100
MAX_PULLS = 50
TITLE_MAX = 200
TIMEOUT_SECONDS = 15
_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitHubSyncError(Exception):
    """Lecture du dépôt impossible : message en français, sans jeton."""


class GitHubRepoClient:
    def __init__(self, repository: str, *, token: str = "", api_url: str = GITHUB_API_URL, timeout: float = TIMEOUT_SECONDS) -> None:
        if not _REPOSITORY.match(repository or ""):
            raise GitHubSyncError(f"Dépôt invalide : {repository!r} (attendu propriétaire/dépôt).")
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "Samae-bot"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, session: aiohttp.ClientSession, path: str, params: dict | None = None):
        async with session.get(f"{self.api_url}/repos/{self.repository}{path}", params=params, headers=self._headers()) as response:
            if is_rate_limited(response.status, response.headers):
                raise GitHubSyncError("Limite de requêtes GitHub atteinte : réessayez dans une heure (ou ajoutez GITHUB_SYNC_TOKEN).")
            if response.status in (401, 403):
                raise GitHubSyncError("GitHub refuse l'accès : vérifiez GITHUB_SYNC_TOKEN (droit de lecture du dépôt).")
            if response.status == 404:
                raise GitHubSyncError(f"Dépôt {self.repository} introuvable (ou privé : il faut GITHUB_SYNC_TOKEN).")
            if response.status == 409:
                return []  # dépôt vide
            if response.status != 200:
                raise GitHubSyncError(f"GitHub a répondu HTTP {response.status} : réessayez plus tard.")
            return await response.json(content_type=None)

    async def fetch(self, *, since: str | None) -> tuple[str, list[dict], list[dict]]:
        """(branche principale, commits de cette branche depuis ``since``, pull requests les plus récemment modifiées)."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                info = await self._get(session, "")
                branch = info.get("default_branch") if isinstance(info, dict) else None
                if not isinstance(branch, str) or not branch:
                    raise GitHubSyncError("Réponse inattendue de GitHub (branche principale absente).")
                params = {"sha": branch, "per_page": MAX_COMMITS}
                if since:
                    params["since"] = since
                commits = await self._get(session, "/commits", params)
                pulls = await self._get(session, "/pulls", {"state": "all", "sort": "updated", "direction": "desc", "per_page": MAX_PULLS})
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise GitHubSyncError(f"GitHub injoignable ({type(exc).__name__}) : réessayez plus tard.") from exc
        return branch, [c for c in commits if isinstance(c, dict)], [p for p in pulls if isinstance(p, dict)]


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Parsed:
    item: SyncItem
    completes: tuple[str, ...]  # éléments annoncés comme terminés (sous-ensemble de item.refs)


# Mot qui, juste avant un identifiant, annonce que l'élément est terminé : « termine FEAT-002 », « closes GP-003 ».
_DONE_WORDS = r"(?:termin[eé]e?s?|finie?s?|finit|achev[eé]e?s?|ferme|closes?|closed|fix(?:es|ed)?|resolves?|resolved|done)"


def completed_ids(text: str, refs: Iterable[str]) -> tuple[str, ...]:
    """Éléments précédés d'un mot de fin (« termine FEAT-002 », « Closes: GP-003 »)."""
    return tuple(ref for ref in refs if re.search(rf"\b{_DONE_WORDS}\s*:?\s*{re.escape(ref)}\b", text, re.IGNORECASE))


def commit_item(repository: str, data: dict, prefixes: Iterable[str]) -> Parsed | None:
    sha = data.get("sha")
    if not isinstance(sha, str) or not _SHA.match(sha):
        return None
    commit = _dict(data.get("commit"))
    message = commit.get("message") if isinstance(commit.get("message"), str) else ""
    author = _dict(data.get("author")).get("login") or _dict(commit.get("author")).get("name") or ""
    refs = tuple(mentioned_ids(message, prefixes))
    item = SyncItem(
        repository, "commit", sha,
        clean_text(first_line(message), TITLE_MAX, single_line=True) or "(sans message)",
        clean_text(author, 80, single_line=True),
        safe_url(data.get("html_url")), "",
        _dict(commit.get("committer")).get("date") or _dict(commit.get("author")).get("date"),
        refs,
    )
    return Parsed(item, completed_ids(message, refs))


def pull_item(repository: str, data: dict, prefixes: Iterable[str]) -> Parsed | None:
    number = data.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    title = data.get("title") if isinstance(data.get("title"), str) else ""
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    branch = _dict(data.get("head")).get("ref") or ""
    state = "merged" if data.get("merged_at") else ("closed" if data.get("state") == "closed" else "open")
    text = f"{title}\n{body}"
    refs = tuple(mentioned_ids(f"{text}\n{branch.upper()}", prefixes))
    item = SyncItem(
        repository, "pull", str(number),
        clean_text(title, TITLE_MAX, single_line=True) or f"Pull request #{number}",
        clean_text(_dict(data.get("user")).get("login") or "", 80, single_line=True),
        safe_url(data.get("html_url")), state,
        data.get("merged_at") or data.get("updated_at"),
        refs,
    )
    return Parsed(item, completed_ids(text, refs))
