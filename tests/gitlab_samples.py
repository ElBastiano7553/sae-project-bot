"""Exemples de JSON GitLab (réduits aux champs utiles), inspirés de la documentation officielle."""

from __future__ import annotations

import copy

PROJECT = {
    "id": 4242,
    "name": "sae-game",
    "path_with_namespace": "epita/sae-game",
    "web_url": "https://gitlab.com/epita/sae-game",
}

_PUSH = {
    "object_kind": "push",
    "event_name": "push",
    "before": "95790bf891e76fee5e1747ab589903a6a1f80f22",
    "after": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
    "ref": "refs/heads/main",
    "user_name": "Alice Martin",
    "user_username": "alice",
    "project_id": 4242,
    "project": PROJECT,
    "commits": [
        {
            "id": "b6568db1bc1dcd7f8b4d5a946b0b91f9dacd7327",
            "message": "Ajoute le menu principal\n\nDétails du commit",
            "title": "Ajoute le menu principal",
            "url": "https://gitlab.com/epita/sae-game/-/commit/b6568db1bc1dcd7f8b4d5a946b0b91f9dacd7327",
            "author": {"name": "Alice Martin", "email": "alice@example.com"},
        },
        {
            "id": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
            "message": "Corrige la collision du joueur",
            "title": "Corrige la collision du joueur",
            "url": "https://gitlab.com/epita/sae-game/-/commit/da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
            "author": {"name": "Bob Durand", "email": "bob@example.com"},
        },
    ],
    "total_commits_count": 2,
}

_MERGE_REQUEST = {
    "object_kind": "merge_request",
    "event_type": "merge_request",
    "user": {"name": "Bob Durand", "username": "bob"},
    "project": PROJECT,
    "object_attributes": {
        "id": 99,
        "iid": 12,
        "title": "Ajout du système de score",
        "description": "Ferme #3",
        "source_branch": "feature/score",
        "target_branch": "main",
        "state": "opened",
        "action": "open",
        "draft": False,
        "url": "https://gitlab.com/epita/sae-game/-/merge_requests/12",
    },
}

_PIPELINE = {
    "object_kind": "pipeline",
    "object_attributes": {
        "id": 31,
        "iid": 3,
        "ref": "main",
        "tag": False,
        "sha": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
        "status": "failed",
        "source": "push",
        "duration": 125,
        "url": "https://gitlab.com/epita/sae-game/-/pipelines/31",
    },
    "user": {"name": "Alice Martin", "username": "alice"},
    "project": PROJECT,
    "commit": {
        "id": "da1560886d4f094c3e6c9ef40349f7d38b5d27d7",
        "message": "Corrige la collision du joueur",
        "title": "Corrige la collision du joueur",
    },
    "builds": [
        {"id": 380, "stage": "test", "name": "pytest", "status": "failed", "allow_failure": False},
        {"id": 381, "stage": "lint", "name": "ruff", "status": "failed", "allow_failure": True},
        {"id": 382, "stage": "build", "name": "build", "status": "success", "allow_failure": False},
    ],
}


def push_payload(**overrides) -> dict:
    return {**copy.deepcopy(_PUSH), **overrides}


def merge_request_payload(**attributes) -> dict:
    payload = copy.deepcopy(_MERGE_REQUEST)
    payload["object_attributes"].update(attributes)
    return payload


def pipeline_payload(**attributes) -> dict:
    payload = copy.deepcopy(_PIPELINE)
    payload["object_attributes"].update(attributes)
    return payload
