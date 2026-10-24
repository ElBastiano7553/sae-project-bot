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
from urllib.parse import urlsplit

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
    # Projet enregistré, sans connexion au fournisseur (GitHub App, OAuth GitLab : étapes B4 et B5).
    "pending": "enregistré, pas encore connecté : aucune notification",
    "active": "actif",
    "suspended": "suspendu",
    "legacy_on": "actif (configuré dans .env)",
    "legacy_error": "désactivé : erreur dans .env",
}

NAME_MAX = 80
GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
# Chemin d'un projet GitLab (« groupe/sous-groupe/projet ») : 2 à 20 segments.
GITLAB_PATH = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}(?:/[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}){1,19}$")
GITLAB_PATH_MAX = 255
GITHUB_HOSTS = {"github.com", "www.github.com"}
URL_MAX = 300

# Projets historiques : identifiants affichés dans /projet (jamais des numéros de la base).
LEGACY_GITHUB = "env-github"
LEGACY_GITLAB = "env-gitlab"

# Événements du projet historique GitHub (src/github) : tous vers le même salon.
LEGACY_GITHUB_CATEGORIES = ("code", "revues", "tickets", "ci")
# Types d'événements GitLab (config/gitlab.toml [routes]) → catégorie commune.
GITLAB_KIND_CATEGORIES = {"push": "code", "merge_request": "revues", "pipeline": "ci", "deployment": "deploiements"}


# Filtres possibles par catégorie, avec un vocabulaire commun à GitHub et GitLab (traduit par
# chaque fournisseur à la réception des événements, étape B3). Une catégorie sans filtre
# enregistré reçoit tout ce que le fournisseur envoie pour elle.
FILTER_CHOICES = {
    "revues": ("actions", ("ouverture", "reouverture", "modification", "fusion", "fermeture", "approbation")),
    "tickets": ("actions", ("ouverture", "fermeture", "reouverture")),
    "ci": ("statuts", ("reussi", "echoue", "annule")),
    "deploiements": ("statuts", ("reussi", "echoue", "annule", "en_cours")),
}
BRANCH_CATEGORIES = ("code", "ci")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/*-]{1,100}$")
BRANCHES_MAX = 20
# Valeur d'une option de filtre qui efface le filtre (retour à « tout »).
CLEAR_FILTER = "toutes"


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
    """Identifiant chez le fournisseur, en minuscules (GitHub et GitLab ignorent la casse : pas de doublon
    « Equipe/Jeu » / « equipe/jeu ») : « owner/depot » (GitHub), chemin « groupe/projet » ou numéro (GitLab)."""
    value = (value or "").strip()
    if check_provider(provider) == "github":
        if not GITHUB_REPOSITORY.match(value):
            raise ProjectError("Dépôt GitHub attendu sous la forme « propriétaire/depot ».")
        return value.casefold()
    if value.isdigit() and int(value) > 0:
        return str(int(value))
    if len(value) <= GITLAB_PATH_MAX and GITLAB_PATH.match(value) and not value.endswith(".git"):
        return value.casefold()
    raise ProjectError("Projet GitLab attendu sous la forme « groupe/projet » (ou son numéro, « Project ID »).")


@dataclass(frozen=True)
class ParsedRepository:
    provider: str
    external_id: str
    instance_url: str
    default_name: str  # dernier segment du chemin, tel qu'écrit dans l'URL


def parse_repository_url(url: str, *, gitlab_hosts: tuple[str, ...] = ()) -> ParsedRepository:
    """Dépôt GitHub ou projet GitLab à partir de son adresse ; le fournisseur est déduit de l'hôte.

    Acceptés : ``https://github.com/owner/depot`` (avec ``.git``, ``/tree/main``… ignorés),
    ``https://gitlab.com/groupe/sous-groupe/projet`` (``/-/…`` ignoré), toute instance GitLab dont
    l'hôte contient « gitlab » ou figure dans ``gitlab_hosts``, et les adresses SSH ``git@hote:chemin.git``.
    Refusés : http://, identifiants dans l'adresse (``jean:motdepasse@``), autres hôtes.
    Aucune requête réseau : l'existence du dépôt n'est pas vérifiée.
    """
    raw = (url or "").strip().strip("<>")
    if not raw or len(raw) > URL_MAX:
        raise ProjectError("Adresse vide ou trop longue. Exemple : https://github.com/equipe/jeu")
    ssh = re.match(r"^git@([A-Za-z0-9.-]+):(.+)$", raw)
    if ssh:
        host, path = ssh.group(1).casefold(), ssh.group(2)
    else:
        parts = urlsplit(raw)
        if parts.scheme != "https":
            raise ProjectError("L'adresse doit commencer par https:// (exemple : https://github.com/equipe/jeu).")
        if parts.username or parts.password or "@" in parts.netloc:
            raise ProjectError(
                "L'adresse contient un identifiant ou un mot de passe : ne collez jamais d'identifiants dans Discord. "
                "Donnez l'adresse publique du dépôt, sans rien avant le nom du site."
            )
        host, path = (parts.hostname or "").casefold(), parts.path
    segments = [s for s in path.split("/") if s]
    if host in GITHUB_HOSTS:
        if len(segments) < 2:
            raise ProjectError("Adresse GitHub incomplète : il faut le propriétaire et le dépôt (https://github.com/equipe/jeu).")
        repo = segments[1].removesuffix(".git")
        external_id = normalize_external_id("github", f"{segments[0]}/{repo}")
        return ParsedRepository("github", external_id, "", repo)
    if "gitlab" in host or host in {h.casefold() for h in gitlab_hosts}:
        if "-" in segments:
            segments = segments[: segments.index("-")]
        if segments:
            segments[-1] = segments[-1].removesuffix(".git")
        if len(segments) < 2:
            raise ProjectError("Adresse GitLab incomplète : il faut le groupe et le projet (https://gitlab.com/groupe/projet).")
        external_id = normalize_external_id("gitlab", "/".join(segments))
        return ParsedRepository("gitlab", external_id, normalize_instance_url("gitlab", f"https://{host}"), segments[-1])
    raise ProjectError(
        "Fournisseur non reconnu : seules les adresses GitHub (github.com) et GitLab (gitlab.com ou une instance GitLab) sont acceptées."
    )


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


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def update_filters(category: str, current: Mapping[str, object], *, branches: str | None = None, values: str | None = None) -> dict:
    """Nouveaux filtres d'une catégorie. ``None`` = option non donnée (filtre inchangé),
    « toutes » = filtre effacé. Lève ``ProjectError`` avec un message affichable."""
    check_category(category)
    filters = dict(current)
    if branches is not None:
        if category not in BRANCH_CATEGORIES:
            raise ProjectError(f"La catégorie « {CATEGORIES[category]} » ne se filtre pas par branche.")
        if branches.strip().casefold() == CLEAR_FILTER:
            filters.pop("branches", None)
        else:
            patterns = list(dict.fromkeys(_split(branches)))
            if not patterns or len(patterns) > BRANCHES_MAX or not all(BRANCH_PATTERN.match(p) for p in patterns):
                raise ProjectError(
                    f"Branches attendues séparées par des virgules (au plus {BRANCHES_MAX}), par exemple « main, develop, feature/* »."
                )
            filters["branches"] = patterns
    if values is not None:
        if category not in FILTER_CHOICES:
            raise ProjectError(f"La catégorie « {CATEGORIES[category]} » n'a pas de filtre d'actions ou de statuts.")
        key, allowed = FILTER_CHOICES[category]
        if values.strip().casefold() == CLEAR_FILTER:
            filters.pop(key, None)
        else:
            chosen = list(dict.fromkeys(v.casefold() for v in _split(values)))
            unknown = [v for v in chosen if v not in allowed]
            if not chosen or unknown:
                raise ProjectError(f"Valeurs possibles pour « {CATEGORIES[category]} » : {', '.join(allowed)} (ou « {CLEAR_FILTER} »).")
            filters[key] = [v for v in allowed if v in chosen]  # ordre stable
    return filters


def describe_filters(filters: Mapping[str, object]) -> str:
    parts = []
    if filters.get("branches"):
        parts.append("branches : " + ", ".join(f"`{b}`" for b in filters["branches"]))
    for key in ("actions", "statuts"):
        if filters.get(key):
            parts.append(f"{key} : " + ", ".join(filters[key]))
    return " · ".join(parts) or "tout"


def registered_view(record, routes: tuple[Route, ...]) -> ProjectView:
    """Vue d'un projet enregistré en base (``database.projects.ProjectRecord``)."""
    if record.provider == "github":
        target = record.external_id
    else:
        label = f"projet n° {record.external_id}" if record.external_id.isdigit() else record.external_id
        target = f"{label} · {record.instance_url}"
    # Aucune connexion au fournisseur n'existe encore (étapes B4 et B5) : un projet actif est « en attente ».
    status = "pending" if record.status == "active" else record.status
    return ProjectView(
        ref=str(record.id),
        guild_id=record.guild_id,
        provider=record.provider,
        name=record.name,
        target=target,
        status=status,
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


def project_visible(project: ProjectView, can_view: Callable[[int], bool]) -> bool:
    """Un membre ordinaire ne voit un projet que s'il a accès à au moins un de ses salons.

    Un projet sans salon, ou dont tous les salons lui sont fermés ou introuvables, est masqué :
    il n'apparaît ni dans les listes, ni dans les détails, ni dans l'autocomplétion.
    """
    return any(r.channel_id is not None and can_view(r.channel_id) for r in project.routes)
