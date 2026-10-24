"""Projets suivis par Samae (multi-projets, étape B1) : modèle commun à GitHub et GitLab.

Un projet appartient à **un** serveur Discord et à **un** fournisseur. Ses notifications
sont rangées par catégories communes aux deux fournisseurs (code, revues, tickets…),
chacune envoyée dans un salon de ce serveur.

Deux sortes de projets :

* les projets **enregistrés** en base (table ``projects``), gérés avec ``/projet`` ;
* les projets **historiques**, configurés dans ``.env`` (``GITHUB_WEBHOOK_*``,
  ``GITLAB_*``) : ils sont seulement affichés, jamais copiés ni modifiés, et
  continuent de fonctionner exactement comme avant.

Aucun token ni secret n'est manipulé ici. Pas d'import de discord : testable seul.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

PROVIDERS = {"github": "GitHub", "gitlab": "GitLab"}

# Catégories communes, dans l'ordre d'affichage. Les clés sont aussi celles de la base.
CATEGORIES = {
    "code": "Code (commits)",
    "revues": "Revues (pull / merge requests)",
    "tickets": "Tickets",
    "ci": "Intégration continue",
    "versions": "Versions",
    "deploiements": "Déploiements",
}

STATUS_LABELS = {
    "active": "actif",
    "suspended": "suspendu",
    "legacy_on": "actif (configuré dans .env)",
    "legacy_error": "désactivé : erreur dans .env",
}

NAME_MAX = 80
GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

# Projets historiques : identifiants affichés dans /projet (jamais des numéros de la base).
LEGACY_GITHUB = "env-github"
LEGACY_GITLAB = "env-gitlab"

# Événements du projet historique GitHub (src/github) : tous vers le même salon.
LEGACY_GITHUB_CATEGORIES = ("code", "revues", "tickets", "ci")
# Types d'événements GitLab (config/gitlab.toml [routes]) → catégorie commune.
GITLAB_KIND_CATEGORIES = {"push": "code", "merge_request": "revues", "pipeline": "ci", "deployment": "deploiements"}


class ProjectError(ValueError):
    """Donnée de projet refusée ; le message est affichable tel quel."""


@dataclass(frozen=True)
class Route:
    category: str
    channel_id: int | None  # None : salon de config/server.toml non associé (projets historiques)
    filters: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectView:
    """Ce que /projet affiche d'un projet, enregistré ou historique."""

    ref: str  # « 12 » pour un projet enregistré, « env-github » / « env-gitlab » pour un historique
    guild_id: int
    provider: str
    name: str
    target: str  # dépôt GitHub ou projet GitLab, tel qu'affiché
    status: str  # clé de STATUS_LABELS
    routes: tuple[Route, ...] = ()
    legacy: bool = False
    created_by: int | None = None
    created_at: str | None = None
    note: str = ""

    @property
    def provider_label(self) -> str:
        return PROVIDERS[self.provider]

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


# --- Validation ------------------------------------------------------------------------------


def check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ProjectError(f"Fournisseur inconnu : {provider!r} (attendu : github ou gitlab).")
    return provider


def check_category(category: str) -> str:
    if category not in CATEGORIES:
        raise ProjectError(f"Catégorie inconnue : {category!r}.")
    return category


def normalize_external_id(provider: str, value: str) -> str:
    """Identifiant chez le fournisseur : « owner/depot » (GitHub) ou numéro de projet (GitLab)."""
    value = (value or "").strip()
    if check_provider(provider) == "github":
        if not GITHUB_REPOSITORY.match(value):
            raise ProjectError("Dépôt GitHub attendu sous la forme « propriétaire/depot ».")
        return value
    if not value.isdigit() or int(value) <= 0:
        raise ProjectError("Numéro de projet GitLab attendu (« Project ID », un nombre).")
    return str(int(value))


def normalize_instance_url(provider: str, value: str) -> str:
    """Adresse de l'instance GitLab (https, sans identifiants) ; toujours vide pour GitHub."""
    if check_provider(provider) == "github":
        return ""
    url = (value or "https://gitlab.com").strip().rstrip("/")
    if not re.match(r"^https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?$", url):
        raise ProjectError("Adresse GitLab attendue sous la forme https://gitlab.exemple.fr (sans identifiants).")
    return url


def normalize_name(value: str) -> str:
    name = " ".join((value or "").split())
    if not 1 <= len(name) <= NAME_MAX:
        raise ProjectError(f"Le nom du projet doit faire entre 1 et {NAME_MAX} caractères.")
    return name


def registered_view(record, routes: tuple[Route, ...]) -> ProjectView:
    """Vue d'un projet enregistré en base (``database.projects.ProjectRecord``)."""
    if record.provider == "github":
        target = record.external_id
    else:
        target = f"projet n° {record.external_id} · {record.instance_url}"
    return ProjectView(
        ref=str(record.id),
        guild_id=record.guild_id,
        provider=record.provider,
        name=record.name,
        target=target,
        status=record.status,
        routes=routes,
        created_by=record.created_by,
        created_at=record.created_at,
    )


# --- Projets historiques (.env) -----------------------------------------------------------------


def legacy_projects(
    settings,
    guild_id: int,
    *,
    channel_id_for: Callable[[str], int | None],
    gitlab_routes: Mapping[str, str] | None,
    github_channel_key: str,
) -> list[ProjectView]:
    """Projets configurés dans ``.env``, rattachés au serveur ``DISCORD_GUILD_ID`` uniquement.

    ``channel_id_for`` : salon associé à une clé de config/server.toml (None si absent).
    ``gitlab_routes`` : [routes] de config/gitlab.toml (None si le fichier est invalide).
    Seuls les noms de variables et l'état sont lus : jamais la valeur d'un secret.
    """
    if guild_id != settings.guild_id:
        return []
    projects: list[ProjectView] = []
    if settings.github_requested:
        channel = channel_id_for(github_channel_key)
        projects.append(
            ProjectView(
                ref=LEGACY_GITHUB,
                guild_id=guild_id,
                provider="github",
                name=f"{settings.github_repository} (historique)",
                target=settings.github_repository,
                status="legacy_error" if settings.github_config_error else "legacy_on",
                routes=tuple(Route(category, channel) for category in LEGACY_GITHUB_CATEGORIES),
                legacy=True,
                note="Réglages dans .env (GITHUB_WEBHOOK_*) ; port " + str(settings.github_webhook_port) + ".",
            )
        )
    if settings.gitlab_requested:
        routes: tuple[Route, ...] = ()
        note = "Réglages dans .env (GITLAB_*) et config/gitlab.toml ; port " + str(settings.webhook_port) + "."
        if gitlab_routes is None:
            note += " config/gitlab.toml est invalide : voir /admin diagnostic."
        else:
            seen: dict[str, Route] = {}
            for kind, category in GITLAB_KIND_CATEGORIES.items():
                if kind in gitlab_routes and category not in seen:
                    seen[category] = Route(category, channel_id_for(gitlab_routes[kind]))
            routes = tuple(seen[c] for c in CATEGORIES if c in seen)
        target = f"projet n° {settings.gitlab_project_id}" if settings.gitlab_project_id else "tout projet (GITLAB_PROJECT_ID vide)"
        projects.append(
            ProjectView(
                ref=LEGACY_GITLAB,
                guild_id=guild_id,
                provider="gitlab",
                name="GitLab (historique)",
                target=f"{target} · {settings.gitlab_base_url}",
                status="legacy_error" if settings.gitlab_config_error else "legacy_on",
                routes=routes,
                legacy=True,
                note=note,
            )
        )
    return projects


# --- Visibilité --------------------------------------------------------------------------------


def visible_routes(routes: tuple[Route, ...], can_view: Callable[[int], bool]) -> tuple[tuple[Route, ...], int]:
    """Salons qu'un membre peut voir, et nombre de salons cachés (privés ou introuvables)."""
    shown = tuple(r for r in routes if r.channel_id is not None and can_view(r.channel_id))
    hidden = sum(1 for r in routes if r.channel_id is not None) - len(shown)
    return shown, hidden
