"""Client minimal de l'API GitLab, en lecture seule (utilisé par /review).

Il suffit d'un token avec le seul scope ``read_api`` (*Project access token* de rôle
Reporter de préférence) : le bot ne modifie jamais rien sur GitLab. Le token est
envoyé dans l'en-tête ``PRIVATE-TOKEN`` uniquement, jamais journalisé ni affiché.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_DIFF_FILES = 100


class GitLabAPIError(Exception):
    """Échec d'un appel à l'API GitLab. Le message est destiné à l'utilisateur."""


@dataclass(frozen=True)
class MergeRequestInfo:
    iid: int
    title: str
    description: str
    author: str
    source_branch: str
    target_branch: str
    state: str
    web_url: str | None


@dataclass(frozen=True)
class FileDiff:
    path: str
    diff: str
    new_file: bool = False
    deleted_file: bool = False
    renamed_from: str | None = None


class GitLabAPI:
    def __init__(self, base_url: str, token: str, project_id: int, *, session: aiohttp.ClientSession | None = None) -> None:
        self._base = f"{base_url.rstrip('/')}/api/v4/projects/{quote(str(project_id), safe='')}"
        self._token = token
        self._session = session
        self._own_session = session is None

    async def merge_request(self, iid: int) -> MergeRequestInfo:
        data = await self._get(f"/merge_requests/{int(iid)}", what=f"la merge request !{iid}")
        if not isinstance(data, dict):
            raise GitLabAPIError("Réponse inattendue de GitLab.")
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        return MergeRequestInfo(
            iid=int(data.get("iid") or iid),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            author=str(author.get("name") or author.get("username") or ""),
            source_branch=str(data.get("source_branch") or ""),
            target_branch=str(data.get("target_branch") or ""),
            state=str(data.get("state") or ""),
            web_url=data.get("web_url") if isinstance(data.get("web_url"), str) else None,
        )

    async def merge_request_diffs(self, iid: int) -> list[FileDiff]:
        data = await self._get(
            f"/merge_requests/{int(iid)}/diffs", params={"per_page": str(MAX_DIFF_FILES)}, what=f"les modifications de !{iid}"
        )
        if not isinstance(data, list):
            raise GitLabAPIError("Réponse inattendue de GitLab.")
        diffs = []
        for item in data[:MAX_DIFF_FILES]:
            if not isinstance(item, dict):
                continue
            old, new = item.get("old_path"), item.get("new_path")
            diffs.append(
                FileDiff(
                    path=str(new or old or "?"),
                    diff=str(item.get("diff") or ""),
                    new_file=bool(item.get("new_file")),
                    deleted_file=bool(item.get("deleted_file")),
                    renamed_from=str(old) if item.get("renamed_file") and old else None,
                )
            )
        return diffs

    async def _get(self, path: str, *, params: dict | None = None, what: str) -> object:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS))
        try:
            async with self._session.get(
                self._base + path, params=params, headers={"PRIVATE-TOKEN": self._token}, allow_redirects=False
            ) as response:
                if response.status == 401:
                    raise GitLabAPIError("GitLab refuse le token (GITLAB_ACCESS_TOKEN invalide ou expiré).")
                if response.status == 403:
                    raise GitLabAPIError("Le token GitLab n'a pas accès à ce projet (scope read_api et rôle Reporter nécessaires).")
                if response.status == 404:
                    raise GitLabAPIError(f"GitLab ne trouve pas {what} (numéro ou GITLAB_PROJECT_ID incorrect ?).")
                if response.status != 200:
                    log.warning("API GitLab : réponse %s pour %s", response.status, path)
                    raise GitLabAPIError(f"GitLab a répondu {response.status} : réessayez plus tard.")
                body = await response.content.read(MAX_RESPONSE_BYTES + 1)
        except aiohttp.ClientError as exc:
            log.warning("API GitLab injoignable : %s", type(exc).__name__)
            raise GitLabAPIError("GitLab est injoignable pour le moment.") from exc
        except TimeoutError as exc:
            raise GitLabAPIError("GitLab ne répond pas (délai dépassé).") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise GitLabAPIError("La réponse de GitLab est trop volumineuse.")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise GitLabAPIError("Réponse illisible de GitLab.") from exc

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None
