"""Règles de notification GitLab lues dans ``config/gitlab.toml``."""

from __future__ import annotations

import fnmatch
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from src.config import CONFIG_DIR, ConfigError, load_toml

EVENT_KINDS = ("push", "merge_request", "pipeline")
MR_ACTIONS = {"open", "reopen", "update", "close", "merge", "approved", "unapproved", "approval", "unapproval"}
PIPELINE_STATUSES = {
    "success", "failed", "canceled", "skipped", "running", "pending", "manual", "created",
    "waiting_for_resource", "preparing", "scheduled",
}
MAX_COMMITS_LIMIT = 10


@dataclass(frozen=True)
class GitLabRules:
    routes: dict[str, str]
    push_branches: tuple[str, ...] = ()
    max_commits: int = 5
    mr_actions: frozenset[str] = frozenset({"open", "reopen", "update", "merge", "close", "approved"})
    pipeline_statuses: frozenset[str] = frozenset({"success", "failed", "canceled"})
    pipeline_branches: tuple[str, ...] = ()

    def follows_push_branch(self, branch: str) -> bool:
        return _matches(branch, self.push_branches)

    def follows_pipeline_branch(self, ref: str) -> bool:
        return _matches(ref, self.pipeline_branches)


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def load_gitlab_rules(path: Path = CONFIG_DIR / "gitlab.toml", channel_keys: Collection[str] | None = None) -> GitLabRules:
    """Charge et valide les règles. ``channel_keys`` : salons connus de config/server.toml."""
    data = load_toml(path)
    name = path.name

    routes_raw = data.get("routes", {})
    routes: dict[str, str] = {}
    for kind in EVENT_KINDS:
        key = routes_raw.get(kind)
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"{name} : [routes] doit indiquer le salon de « {kind} ».")
        key = key.strip()
        if channel_keys is not None and key not in channel_keys:
            raise ConfigError(f"{name} : [routes] {kind} = « {key} » n'est pas un salon de config/server.toml.")
        routes[kind] = key

    push = data.get("push", {})
    mr = data.get("merge_request", {})
    pipeline = data.get("pipeline", {})

    max_commits = push.get("max_commits", 5)
    if not isinstance(max_commits, int) or isinstance(max_commits, bool) or not 1 <= max_commits <= MAX_COMMITS_LIMIT:
        raise ConfigError(f"{name} : [push] max_commits doit être un nombre entre 1 et {MAX_COMMITS_LIMIT}.")

    return GitLabRules(
        routes=routes,
        push_branches=_str_list(push, "branches", f"{name} : [push]"),
        max_commits=max_commits,
        mr_actions=frozenset(_choices(mr, "actions", MR_ACTIONS, f"{name} : [merge_request]", ["open", "reopen", "update", "merge", "close", "approved"])),
        pipeline_statuses=frozenset(_choices(pipeline, "statuses", PIPELINE_STATUSES, f"{name} : [pipeline]", ["success", "failed", "canceled"])),
        pipeline_branches=_str_list(pipeline, "branches", f"{name} : [pipeline]"),
    )


def _str_list(table: dict, key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{where} {key} doit être une liste de textes, par exemple [\"main\"].")
    return tuple(item.strip() for item in value)


def _choices(table: dict, key: str, allowed: set[str], where: str, default: list[str]) -> tuple[str, ...]:
    values = _str_list({key: table.get(key, default)}, key, where)
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"{where} {key} contient des valeurs inconnues : {', '.join(unknown)}.")
    return values
