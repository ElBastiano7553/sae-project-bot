"""Traduction des événements GitHub et GitLab dans le vocabulaire commun des projets.

La mise en forme reste celle des webhooks historiques (``src/github/events.py``,
``src/gitlab/events.py``) : mêmes notifications, même nettoyage des textes reçus.
Ce module ajoute seulement ce qu'il faut pour aiguiller et filtrer : la catégorie
commune, l'action ou le statut (``services.projects.FILTER_CHOICES``) et la branche.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.github import events as github_events
from src.gitlab import events as gitlab_events
from src.gitlab.events import Notification
from src.gitlab.rules import EVENT_KINDS, MR_ACTIONS, GitLabRules

# Catégorie commune de chaque type de notification.
CATEGORY_OF_KIND = {
    "github_push": "code",
    "github_pull_request": "revues",
    "github_issue": "tickets",
    "github_workflow": "ci",
    "push": "code",
    "merge_request": "revues",
    "pipeline": "ci",
    "deployment": "deploiements",
}

# Action ou statut du fournisseur → vocabulaire commun. Absent : pas d'équivalent (écarté par un filtre).
COMMON_VALUES = {
    "revues": {
        # GitHub
        "opened": "ouverture",
        "reopened": "reouverture",
        "ready_for_review": "modification",
        "closed": "fermeture",
        # GitLab
        "open": "ouverture",
        "reopen": "reouverture",
        "update": "modification",
        "close": "fermeture",
        "approved": "approbation",
        "approval": "approbation",
        # Les deux
        "merge": "fusion",
    },
    "tickets": {"opened": "ouverture", "closed": "fermeture", "reopened": "reouverture"},
    "ci": {
        "success": "reussi",
        "failed": "echoue",
        "canceled": "annule",
        # GitHub Actions (seuls les échecs sont mis en forme, voir src/github/events.py)
        "failure": "echoue",
        "timed_out": "echoue",
        "startup_failure": "echoue",
        "cancelled": "annule",
    },
    "deploiements": {"success": "reussi", "failed": "echoue", "canceled": "annule", "running": "en_cours"},
}

# Règles GitLab sans filtre : chaque projet applique ensuite ses propres filtres. « Tout » couvre
# le vocabulaire commun (pas les pipelines ignorés ou manuels, ni les déploiements simplement créés).
OPEN_GITLAB_RULES = GitLabRules(
    routes={kind: "projet" for kind in EVENT_KINDS},
    mr_actions=frozenset(MR_ACTIONS),
    pipeline_statuses=frozenset({"success", "failed", "canceled"}),
    deployment_statuses=frozenset({"success", "failed", "canceled", "running"}),
)

GITHUB_EVENTS = github_events.SUPPORTED_EVENTS
GITLAB_EVENTS = gitlab_events.SUPPORTED_EVENTS  # en-tête X-Gitlab-Event → type


@dataclass(frozen=True)
class ProjectEvent:
    provider: str
    event: str  # type d'événement (en-tête GitHub, ou type GitLab : push, merge_request…)
    delivery_id: str
    category: str
    value: str | None  # action ou statut commun
    branch: str | None
    notification: Notification

    @property
    def delivery_key(self) -> str:
        return f"{self.provider}:{self.event}:{self.delivery_id}"

    @property
    def dedup_key(self) -> str | None:
        return self.notification.dedup_key


def _event(provider: str, event: str, delivery_id: str, notification: Notification | None) -> ProjectEvent | None:
    if notification is None:
        return None
    category = CATEGORY_OF_KIND[notification.kind]
    value = COMMON_VALUES.get(category, {}).get(notification.action)
    return ProjectEvent(provider, event, delivery_id, category, value, notification.ref or None, notification)


def translate_github(event: str, delivery_id: str, payload: dict) -> ProjectEvent | None:
    """None : événement non géré ou sans intérêt. Lève ``InvalidPayload`` si le JSON est incomplet."""
    if event not in GITHUB_EVENTS:
        return None
    return _event("github", event, delivery_id, github_events.format_event(event, payload))


def translate_gitlab(kind: str, delivery_id: str, payload: dict) -> ProjectEvent | None:
    """``kind`` : push, merge_request, pipeline ou deployment. Lève ``InvalidPayload`` si le JSON est incomplet."""
    return _event("gitlab", kind, delivery_id, gitlab_events.format_event(kind, payload, OPEN_GITLAB_RULES))
