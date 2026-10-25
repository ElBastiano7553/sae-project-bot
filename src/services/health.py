"""Rapport d'état utilisé par la commande /etat et /admin diagnostic.

Chaque ligne décrit uniquement ce qui a réellement été vérifié par le bot, au
moment de la commande. Une ligne dit explicitement quand un service n'est pas
vérifié (par exemple la joignabilité des webhooks depuis Internet) : répondre à
/etat ne prouve pas que le bot est disponible en continu, ce que seule une
surveillance externe peut garantir.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.services.monitoring import SLOW_INTERACTION_MS, InteractionSummary
from src.services.server_structure import StructureReport


class HealthStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    DISABLED = "disabled"


HEALTH_ICONS = {
    HealthStatus.OK: "✅",
    HealthStatus.WARNING: "⚠️",
    HealthStatus.ERROR: "❌",
    HealthStatus.DISABLED: "⏸️",
}


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: HealthStatus
    detail: str


def build_health_checks(
    *,
    latency_ms: float | None,
    database_ok: bool,
    guild_found: bool | None,
    structure: StructureReport | None,
    setup_confirmed: bool,
    ai_provider: str,
    gitlab_enabled: bool = False,
    gitlab_listening: bool = False,
    gitlab_error: str | None = None,
    gitlab_last_event_at: str | None = None,
    database_schema: int | None = None,
    messages: MessagesSummary | None = None,
    gitlab_address: str | None = None,
    alerts: AlertsSummary | None = None,
    history: HistorySummary | None = None,
    ai: AISummary | None = None,
    backups: BackupSummary | None = None,
    interactions: InteractionSummary | None = None,
    github: GitHubSummary | None = None,
    projects: ProjectsReceptionSummary | None = None,
    team_space: bool = True,
) -> list[HealthCheck]:
    """Construit la liste des vérifications à partir de valeurs déjà mesurées.

    ``guild_found`` : None = aucun serveur principal (facultatif), True/False = le bot y est ou non.
    ``team_space=False`` (autre serveur que le serveur principal) : seules les lignes utiles à ce serveur sont
    gardées (Discord, base, réactivité) ; l'espace d'équipe d'un autre serveur n'y est jamais décrit.
    """
    db_detail = "SQLite répond" if database_ok else "SQLite ne répond pas : consultez logs/bot.log"
    if database_ok and database_schema is not None:
        db_detail += f" (schéma v{database_schema})"
    checks = [
        _discord_check(latency_ms),
        HealthCheck("Base de données", HealthStatus.OK if database_ok else HealthStatus.ERROR, db_detail),
        _home_check(guild_found, team_space),
        _structure_check(structure),
        _messages_check(setup_confirmed, messages),
        _gitlab_check(gitlab_enabled, gitlab_listening, gitlab_error, gitlab_last_event_at, gitlab_address),
        _ai_check(ai or AISummary(provider=ai_provider)),
    ]
    if not team_space:
        checks = checks[:3]
        if interactions is not None:
            checks.append(_interactions_check(interactions))
        return checks
    if github is not None:
        checks.insert(-1, _github_check(github))
    if projects is not None:
        checks.insert(-1, _projects_check(projects))
    if history is not None:
        checks.append(_history_check(history))
    if alerts is not None:
        checks.append(_alerts_check(alerts))
    if backups is not None:
        checks.append(_backup_check(backups))
    if interactions is not None:
        checks.append(_interactions_check(interactions))
    return checks


def _home_check(guild_found: bool | None, team_space: bool) -> HealthCheck:
    name = "Serveur principal"
    if guild_found is None:
        return HealthCheck(name, HealthStatus.DISABLED, "aucun (facultatif : seulement pour l'espace d'équipe, voir /setup serveur-principal)")
    if not team_space:
        return HealthCheck(name, HealthStatus.OK, "un autre serveur : ici, réunions, /projet et /aide seulement")
    if guild_found:
        return HealthCheck(name, HealthStatus.OK, "ce serveur (espace d'équipe disponible)")
    return HealthCheck(name, HealthStatus.ERROR, "le bot n'est pas sur son serveur principal (DISCORD_GUILD_ID ou /setup)")


def _interactions_check(summary: InteractionSummary) -> HealthCheck:
    name = "Réactivité"
    if not summary.total:
        return HealthCheck(name, HealthStatus.OK, "aucune commande depuis le démarrage")
    detail = f"{summary.total} commande(s) depuis le démarrage, médiane {summary.median_ms} ms"
    if summary.slow:
        detail += f", {summary.slow} lente(s) (> {SLOW_INTERACTION_MS / 1000:.1f} s, la plus lente : {summary.slowest})"
    if summary.errors:
        detail += f", {summary.errors} en erreur (surtout /{summary.failing})"
    # Quelques lenteurs isolées (réseau) sont normales ; une sur dix mérite d'être regardée.
    worrying = summary.total >= 5 and (summary.slow + summary.errors) * 10 > summary.total
    return HealthCheck(name, HealthStatus.WARNING if worrying else HealthStatus.OK, detail)


@dataclass(frozen=True)
class BackupSummary:
    interval_hours: int  # 0 = sauvegardes automatiques désactivées
    last_age_hours: float | None  # None = aucune sauvegarde
    count: int = 0


def _backup_check(summary: BackupSummary) -> HealthCheck:
    name = "Sauvegardes"
    if summary.last_age_hours is None:
        status = HealthStatus.WARNING
        detail = "aucune sauvegarde : `/admin sauvegarde`"
    else:
        detail = f"dernière il y a {summary.last_age_hours:.0f} h, {summary.count} conservée(s)"
        late = summary.interval_hours and summary.last_age_hours > 2 * summary.interval_hours
        status = HealthStatus.WARNING if late else HealthStatus.OK
        if late:
            detail += " : en retard, voir logs/bot.log"
    if not summary.interval_hours:
        detail += " (automatiques désactivées : BACKUP_INTERVAL_HOURS=0)"
    return HealthCheck(name, status, detail)


@dataclass(frozen=True)
class AISummary:
    provider: str  # "none", "anthropic", "fake"
    model: str | None = None
    config_error: str | None = None
    used_today: int = 0
    daily_limit: int = 0
    gitlab_api: bool = False  # /ia relecture possible


def _ai_check(summary: AISummary) -> HealthCheck:
    name = "IA générative"
    if summary.config_error:
        return HealthCheck(name, HealthStatus.WARNING, f"désactivée, configuration invalide : {summary.config_error}")
    if summary.provider == "none":
        return HealthCheck(name, HealthStatus.DISABLED, "désactivée (AI_PROVIDER=none)")
    usage = f"{summary.used_today}/{summary.daily_limit} demandes aujourd'hui"
    review = "" if summary.gitlab_api else " ; /ia relecture indisponible (API GitLab non configurée)"
    if summary.provider == "fake":
        return HealthCheck(name, HealthStatus.WARNING, f"faux fournisseur de test (AI_PROVIDER=fake) : aucune vraie IA, {usage}{review}")
    # La clé n'est pas testée ici : un appel coûterait une demande. Une clé refusée apparaît dans « Dernière erreur ».
    return HealthCheck(name, HealthStatus.OK, f"Claude ({summary.model or 'modèle par défaut'}), {usage}{review}")


@dataclass(frozen=True)
class AlertsSummary:
    channel_key: str | None  # ALERTS_CHANNEL
    channel_bound: bool  # salon associé par /admin installer
    errors_24h: int  # erreurs distinctes vues dans les dernières 24 heures


@dataclass(frozen=True)
class HistorySummary:
    retention_days: int
    events: int
    first_event_at: str | None
    deployments: int


def _alerts_check(summary: AlertsSummary) -> HealthCheck:
    errors = f"{summary.errors_24h} erreur(s) distincte(s) en 24 h"
    if not summary.channel_key:
        return HealthCheck("Alertes", HealthStatus.DISABLED, f"désactivées (ALERTS_CHANNEL vide), {errors}")
    if not summary.channel_bound:
        return HealthCheck(
            "Alertes", HealthStatus.WARNING, f"salon « {summary.channel_key} » non associé : vérifiez ALERTS_CHANNEL puis /admin installer"
        )
    return HealthCheck("Alertes", HealthStatus.OK, f"actives dans « {summary.channel_key} », {errors}")


def _history_check(summary: HistorySummary) -> HealthCheck:
    if not summary.events:
        detail = "aucun événement enregistré"
    else:
        detail = f"{summary.events} événement(s) depuis le {summary.first_event_at[:10]}, dont {summary.deployments} déploiement(s)"
    return HealthCheck("Historique", HealthStatus.OK, f"{detail} ; conservé {summary.retention_days} jours")


@dataclass(frozen=True)
class MessagesSummary:
    """État des messages permanents d'après la base (dernière vérification enregistrée)."""

    expected: int
    published: int
    errors: int = 0
    provisional: int = 0


def _messages_check(setup_confirmed: bool, summary: MessagesSummary | None) -> HealthCheck:
    name = "Messages permanents"
    if not setup_confirmed:
        return HealthCheck(name, HealthStatus.WARNING, "lancez /admin installer puis confirmez")
    if summary is None:
        return HealthCheck(name, HealthStatus.OK, "configuration confirmée")
    detail = f"{summary.published}/{summary.expected} publiés"
    if summary.provisional:
        detail += f", {summary.provisional} avec des liens à compléter"
    if summary.errors:
        return HealthCheck(name, HealthStatus.ERROR, f"{detail}, {summary.errors} en erreur : voir /admin messages")
    if summary.published < summary.expected:
        return HealthCheck(name, HealthStatus.WARNING, f"{detail} : lancez /admin actualiser")
    return HealthCheck(name, HealthStatus.OK, detail)


def overall_status(checks: list[HealthCheck]) -> HealthStatus:
    statuses = {check.status for check in checks}
    if HealthStatus.ERROR in statuses:
        return HealthStatus.ERROR
    if HealthStatus.WARNING in statuses:
        return HealthStatus.WARNING
    return HealthStatus.OK


def _discord_check(latency_ms: float | None) -> HealthCheck:
    if latency_ms is None or latency_ms != latency_ms:  # NaN tant que la connexion n'est pas établie
        return HealthCheck("Connexion Discord", HealthStatus.WARNING, "latence inconnue")
    status = HealthStatus.OK if latency_ms < 1000 else HealthStatus.WARNING
    return HealthCheck("Connexion Discord", status, f"latence {latency_ms:.0f} ms")


def _gitlab_check(
    enabled: bool, listening: bool, error: str | None, last_event_at: str | None, address: str | None = None
) -> HealthCheck:
    if not enabled:
        return HealthCheck("GitLab", HealthStatus.DISABLED, "désactivé (GITLAB_WEBHOOK_SECRET vide)")
    if error or not listening:
        return HealthCheck("GitLab", HealthStatus.ERROR, error or "serveur de webhooks arrêté")
    last = f"dernier événement : {last_event_at} UTC" if last_event_at else "aucun événement reçu pour l'instant"
    where = f" sur {address}" if address else ""
    # Le bot sait seulement qu'il écoute localement : il ne peut pas vérifier que GitLab le joint.
    return HealthCheck("GitLab", HealthStatus.OK, f"webhooks actifs{where}, {last} (accès depuis Internet non vérifié)")


@dataclass(frozen=True)
class GitHubSummary:
    """État des webhooks GitHub (dépôt du jeu), mesuré au moment de /etat."""

    enabled: bool = False  # configurés, même de façon invalide
    listening: bool = False
    error: str | None = None
    last_event_at: str | None = None
    address: str | None = None
    repository: str = ""


def _github_check(summary: GitHubSummary) -> HealthCheck:
    if not summary.enabled:
        return HealthCheck("GitHub", HealthStatus.DISABLED, "désactivé (GITHUB_WEBHOOK_SECRET vide)")
    if summary.error or not summary.listening:
        return HealthCheck("GitHub", HealthStatus.ERROR, summary.error or "serveur de webhooks arrêté")
    last = f"dernier événement : {summary.last_event_at} UTC" if summary.last_event_at else "aucun événement reçu pour l'instant"
    where = f" sur {summary.address}" if summary.address else ""
    repo = f" pour {summary.repository}" if summary.repository else ""
    # Comme pour GitLab : le bot sait seulement qu'il écoute localement.
    return HealthCheck("GitHub", HealthStatus.OK, f"webhooks actifs{where}{repo}, {last} (accès depuis Internet non vérifié)")


@dataclass(frozen=True)
class ProjectsReceptionSummary:
    """Réception commune des projets enregistrés (multi-projets, étape B3), mesurée au moment de /etat."""

    enabled: bool = False
    listening: bool = False
    error: str | None = None
    address: str = ""
    github_app: bool = False  # chemin /github/webhook ouvert (GITHUB_APP_WEBHOOK_SECRET défini)
    last_event_at: str | None = None
    github_connect: bool = False  # connexion des dépôts GitHub (étape B4) active
    github_connect_error: str | None = None  # configuration de la connexion GitHub refusée
    gitlab_connect: bool = False  # connexion des projets GitLab (étape B5) active
    gitlab_connect_error: str | None = None  # configuration de la connexion GitLab refusée


def _projects_check(summary: ProjectsReceptionSummary) -> HealthCheck:
    name = "Réception des projets"
    if summary.error:
        return HealthCheck(name, HealthStatus.ERROR, summary.error)
    if not summary.enabled:
        return HealthCheck(name, HealthStatus.DISABLED, "désactivée (PROJECTS_HTTP_ENABLED)")
    if not summary.listening:
        return HealthCheck(name, HealthStatus.ERROR, "serveur arrêté")
    github = "GitHub App ouverte" if summary.github_app else "GitHub App fermée (GITHUB_APP_WEBHOOK_SECRET vide)"
    last = f"dernier événement : {summary.last_event_at} UTC" if summary.last_event_at else "aucun événement reçu"
    status = HealthStatus.OK
    connections = []
    for label, active, error in (
        ("GitHub", summary.github_connect, summary.github_connect_error),
        ("GitLab", summary.gitlab_connect, summary.gitlab_connect_error),
    ):
        if error:
            connections.append(f"connexion {label} refusée ({error})")
            status = HealthStatus.WARNING
        else:
            connections.append(f"connexion {label} active" if active else f"connexion {label} non configurée")
    connect = ", ".join(connections)
    return HealthCheck(
        name, status,
        # Aucun nombre de projets ici : /etat est ouvert à tous, et un projet peut être masqué à un membre.
        f"écoute locale sur {summary.address}, {github}, {connect}, {last} (accès depuis Internet non vérifié)",
    )


def _structure_check(structure: StructureReport | None) -> HealthCheck:
    if structure is None:
        return HealthCheck("Salons", HealthStatus.ERROR, "structure non vérifiée")
    total = len(structure.channels)
    usable = len(structure.bindings)
    if structure.is_complete:
        return HealthCheck("Salons", HealthStatus.OK, f"{usable}/{total} salons reconnus")
    return HealthCheck(
        "Salons", HealthStatus.WARNING, f"{usable}/{total} salons utilisables, détails avec /admin installer"
    )
