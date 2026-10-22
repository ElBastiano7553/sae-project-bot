"""Exemples de JSON GitHub (réduits aux champs utiles), pour les tests et `python -m src.github.demo`."""

from __future__ import annotations

import copy

REPOSITORY = {
    "id": 900001,
    "name": "game-project",
    "full_name": "ElBastiano7553/game-project",
    "private": True,
    "html_url": "https://github.com/ElBastiano7553/game-project",
    "default_branch": "main",
}

SENDER = {"login": "alice", "id": 101}

_PUSH = {
    "ref": "refs/heads/main",
    "before": "95790bf891e76fee5e1747ab589903a6a1f80f22",
    "after": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
    "created": False,
    "deleted": False,
    "forced": False,
    "compare": "https://github.com/ElBastiano7553/game-project/compare/95790bf891e7...da1560886d4f",
    "repository": REPOSITORY,
    "pusher": {"name": "alice", "email": "alice@example.com"},
    "sender": SENDER,
    "commits": [
        {
            "id": "b6568db1bc1dcd7f8b4d5a946b0b91f9dacd7327",
            "distinct": True,
            "message": "Ajoute le menu principal\n\nDétails du commit",
            "timestamp": "2026-10-22T14:58:00+02:00",
            "url": "https://github.com/ElBastiano7553/game-project/commit/b6568db1bc1dcd7f8b4d5a946b0b91f9dacd7327",
            "author": {"name": "Alice Martin", "email": "alice@example.com", "username": "alice"},
            "added": ["src/menu.py"],
            "modified": ["src/main.py"],
            "removed": [],
        },
        {
            "id": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
            "distinct": True,
            "message": "Corrige la collision du joueur",
            "timestamp": "2026-10-22T15:04:00+02:00",
            "url": "https://github.com/ElBastiano7553/game-project/commit/da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
            "author": {"name": "Bob Durand", "email": "bob@example.com", "username": "bob"},
            "added": [],
            "modified": ["src/player.py", "src/main.py"],
            "removed": ["src/old_collision.py"],
        },
    ],
}

_PULL_REQUEST = {
    "action": "opened",
    "number": 12,
    "repository": REPOSITORY,
    "sender": {"login": "bob", "id": 102},
    "pull_request": {
        "number": 12,
        "title": "Ajout du système de score",
        "body": "Ferme #3",
        "html_url": "https://github.com/ElBastiano7553/game-project/pull/12",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "closed_at": None,
        "updated_at": "2026-10-22T13:10:00Z",
        "head": {"ref": "feature/score", "sha": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7"},
        "base": {"ref": "main", "sha": "95790bf891e76fee5e1747ab589903a6a1f80f22"},
        "user": {"login": "bob", "id": 102},
        "requested_reviewers": [{"login": "alice", "id": 101}],
        "assignees": [{"login": "bob", "id": 102}],
        "commits": 3,
        "additions": 120,
        "deletions": 14,
        "changed_files": 5,
    },
}

_ISSUE = {
    "action": "opened",
    "repository": REPOSITORY,
    "sender": {"login": "carole", "id": 103},
    "issue": {
        "number": 7,
        "title": "Le joueur traverse les murs",
        "body": "Reproduit au niveau 2.",
        "html_url": "https://github.com/ElBastiano7553/game-project/issues/7",
        "state": "open",
        "state_reason": None,
        "updated_at": "2026-10-22T15:20:00Z",
        "closed_at": None,
        "labels": [{"name": "bug"}, {"name": "niveau-2"}],
        "assignees": [{"login": "alice", "id": 101}],
        "user": {"login": "carole", "id": 103},
    },
}

_WORKFLOW_RUN = {
    "action": "completed",
    "repository": REPOSITORY,
    "sender": SENDER,
    "workflow": {"id": 55, "name": "Tests"},
    "workflow_run": {
        "id": 777001,
        "name": "Tests",
        "run_number": 42,
        "run_attempt": 1,
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "head_branch": "main",
        "head_sha": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
        "display_title": "Corrige la collision du joueur",
        "html_url": "https://github.com/ElBastiano7553/game-project/actions/runs/777001",
        "run_started_at": "2026-10-22T15:05:00Z",
        "updated_at": "2026-10-22T15:07:30Z",
        "actor": SENDER,
        "triggering_actor": SENDER,
    },
}

_PING = {
    "zen": "Keep it logically awesome.",
    "hook_id": 1,
    "hook": {"type": "Repository", "events": ["push", "pull_request", "issues", "workflow_run"]},
    "repository": REPOSITORY,
    "sender": SENDER,
}


def push_payload() -> dict:
    return copy.deepcopy(_PUSH)


def pull_request_payload() -> dict:
    return copy.deepcopy(_PULL_REQUEST)


def issue_payload() -> dict:
    return copy.deepcopy(_ISSUE)


def workflow_run_payload() -> dict:
    return copy.deepcopy(_WORKFLOW_RUN)


def ping_payload() -> dict:
    return copy.deepcopy(_PING)
