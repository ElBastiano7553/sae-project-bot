"""Diagnostic d'un projet enregistré : pourquoi ses notifications n'arrivent pas (``/projet diagnostic``).

Les contrôles suivent le chemin d'un événement, dans l'ordre : réception de Samae, lien avec le
fournisseur, salons, puis ce qui a réellement été reçu pour CE projet. Chaque ligne ne dit que ce
que Samae a vérifié ; ce qu'il ne peut pas voir (Nginx, DNS, réglages côté GitHub ou GitLab) est
indiqué comme une piste, jamais comme un fait. Aucun secret, jeton ni contenu d'événement n'est lu.

Ce module ne dépend pas de discord.py : le cog lui donne les faits déjà vérifiés.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.services.health import HealthCheck, HealthStatus, overall_status
from src.services.projects import CATEGORIES

# Fenêtre des compteurs de livraisons affichés.
DIAGNOSIS_DAYS = 7

DELIVERY_LABELS = {
    "sent": "publiée(s)",
    "queued": "en attente d'envoi",
    "filtered": "écartée(s) par les filtres",
    "skipped": "salon refusé ou introuvable",
    "failed": "envoi impossible",
}


@dataclass(frozen=True)
class ReceptionFacts:
    enabled: bool  # PROJECTS_HTTP_ENABLED
    error: str | None  # configuration refusée ou démarrage raté
    listening: bool
    github_open: bool  # GITHUB_APP_WEBHOOK_SECRET défini : POST /github/webhook existe


@dataclass(frozen=True)
class DeliveryFacts:
    counts: Mapping[str, int]  # statut → nombre, sur DIAGNOSIS_DAYS jours
    last_received_at: str | None  # dernière livraison enregistrée pour ce projet (UTC), toutes dates


@dataclass(frozen=True)
class ProjectFacts:
    provider: str  # github ou gitlab
    status: str  # projects.status (active, suspended…)
    linked: bool  # lien créé par /projet configurer (GitHub, ou GitLab avec projet:)
    installation_known: bool  # GitHub : l'installation reliée est vérifiée pour CE serveur
    legacy_duplicate: bool  # même dépôt que le webhook historique du .env encore actif
    routes: Sequence[tuple[str, str]]  # (catégorie, problème du salon ou "")


@dataclass(frozen=True)
class Diagnosis:
    checks: tuple[HealthCheck, ...]
    hint: str  # première chose à faire, en une phrase

    @property
    def status(self) -> HealthStatus:
        return overall_status(list(self.checks))


def _reception(provider: str, reception: ReceptionFacts) -> HealthCheck:
    name = "Réception de Samae"
    if reception.error:
        return HealthCheck(name, HealthStatus.ERROR, f"refusée : {reception.error}")
    if not reception.enabled:
        return HealthCheck(name, HealthStatus.ERROR, "désactivée (PROJECTS_HTTP_ENABLED n'est pas à true sur le serveur qui héberge Samae)")
    if not reception.listening:
        return HealthCheck(name, HealthStatus.ERROR, "arrêtée : le port est peut-être déjà utilisé, voir /etat et logs/bot.log")
    if provider == "github" and not reception.github_open:
        return HealthCheck(name, HealthStatus.ERROR, "fermée pour GitHub : GITHUB_APP_WEBHOOK_SECRET est vide")
    return HealthCheck(name, HealthStatus.OK, "active en local ; l'accès depuis Internet (Nginx, HTTPS) n'est pas vérifiable d'ici")


def _link(project: ProjectFacts) -> HealthCheck:
    name = "Connexion au fournisseur"
    if project.status != "active":
        return HealthCheck(name, HealthStatus.ERROR, f"projet « {project.status} » : ses événements sont ignorés")
    if not project.linked:
        how = "`/projet configurer`" if project.provider == "github" else "`/projet configurer projet:`"
        return HealthCheck(name, HealthStatus.ERROR, f"projet enregistré mais jamais connecté : utilisez {how}")
    if project.provider == "github" and not project.installation_known:
        return HealthCheck(
            name, HealthStatus.WARNING,
            "relié à une installation de la GitHub App qui n'est plus vérifiée pour ce serveur : relancez `/projet configurer`",
        )
    if project.legacy_duplicate:
        return HealthCheck(
            name, HealthStatus.WARNING,
            "connecté, mais le webhook historique du .env suit le même dépôt : notifications en double possibles",
        )
    return HealthCheck(name, HealthStatus.OK, "connecté")


def _routes(project: ProjectFacts) -> HealthCheck:
    name = "Salons"
    if not project.routes:
        return HealthCheck(name, HealthStatus.ERROR, "aucun salon choisi : `/projet modifier` (les événements sans salon ne sont pas enregistrés)")
    problems = [f"{CATEGORIES.get(category, category)} : {problem}" for category, problem in project.routes if problem]
    if problems:
        return HealthCheck(name, HealthStatus.ERROR, " ; ".join(problems))
    labels = ", ".join(CATEGORIES.get(category, category) for category, _ in project.routes)
    return HealthCheck(name, HealthStatus.OK, f"Samae peut publier pour : {labels}")


def _deliveries(deliveries: DeliveryFacts) -> HealthCheck:
    name = f"Reçu ces {DIAGNOSIS_DAYS} derniers jours"
    counts = {status: count for status, count in deliveries.counts.items() if count}
    if not counts:
        last = f" (dernier : {deliveries.last_received_at} UTC)" if deliveries.last_received_at else ""
        return HealthCheck(name, HealthStatus.WARNING, f"aucun événement enregistré pour ce projet{last}")
    parts = [f"{counts[status]} {label}" for status, label in DELIVERY_LABELS.items() if status in counts]
    status = HealthStatus.WARNING if counts.keys() & {"failed", "skipped"} else HealthStatus.OK
    return HealthCheck(name, status, ", ".join(parts) + f" ; dernier : {deliveries.last_received_at} UTC")


def _hint(provider: str, checks: Sequence[HealthCheck], deliveries: DeliveryFacts) -> str:
    for check in checks:
        if check.status is HealthStatus.ERROR:
            return f"À corriger d'abord : {check.name.lower()}, {check.detail}."
    counts = deliveries.counts
    if counts.get("failed") or counts.get("skipped"):
        return "Des notifications n'ont pas pu être publiées : vérifiez le salon et les permissions de Samae (« Voir », « Envoyer », « Intégrer des liens »)."
    if not any(counts.values()):
        if provider == "github":
            return (
                "Samae n'a rien reçu pour ce projet. Sur GitHub, ouvrez les réglages de la GitHub App → Advanced → "
                "Recent deliveries : une livraison en échec (502, délai) montre que GitHub n'atteint pas Samae (Nginx, HTTPS) ; "
                "GitHub ne la renvoie pas tout seul, utilisez « Redeliver ». Seuls les événements d'une catégorie qui a un salon sont enregistrés."
            )
        return (
            "Samae n'a rien reçu pour ce projet. Dans GitLab, Paramètres → Webhooks → Modifier → Événements récents : "
            "une erreur montre que GitLab n'atteint pas Samae ; GitLab désactive un webhook qui échoue plusieurs fois de suite."
        )
    if counts.get("filtered") and not counts.get("sent"):
        return "Tout ce qui a été reçu a été écarté par les filtres : vérifiez-les avec `/projet details`, puis `/projet modifier`."
    return "Rien d'anormal vu par Samae. Le message de test de `/projet configurer` vérifie la publication de bout en bout."


def diagnose(project: ProjectFacts, reception: ReceptionFacts, deliveries: DeliveryFacts) -> Diagnosis:
    checks = (_reception(project.provider, reception), _link(project), _routes(project), _deliveries(deliveries))
    return Diagnosis(checks, _hint(project.provider, checks, deliveries))
