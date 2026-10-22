"""Rapport d'état utilisé par les commandes /status et /health.

Chaque ligne décrit uniquement ce qui a réellement été vérifié par le bot, au
moment de la commande. Une ligne dit explicitement quand un service n'est pas
vérifié (par exemple la joignabilité des webhooks depuis Internet) : répondre à
/status ne prouve pas que le bot est disponible en continu, ce que seule une
surveillance externe peut garantir.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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
    guild_found: bool,
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
) -> list[HealthCheck]:
    """Construit la liste des vérifications à partir de valeurs déjà mesurées."""
    db_detail = "SQLite répond" if database_ok else "SQLite ne répond pas : consultez logs/bot.log"
    if database_ok and database_schema is not None:
        db_detail += f" (schéma v{database_schema})"
    checks = [
        _discord_check(latency_ms),
        HealthCheck("Base de données", HealthStatus.OK if database_ok else HealthStatus.ERROR, db_detail),
        HealthCheck(
            "Serveur Discord",
            HealthStatus.OK if guild_found else HealthStatus.ERROR,
            "serveur trouvé" if guild_found else "le bot n'est pas sur le serveur DISCORD_GUILD_ID",
        ),
        _structure_check(structure),
        _messages_check(setup_confirmed, messages),
        _gitlab_check(gitlab_enabled, gitlab_listening, gitlab_error, gitlab_last_event_at, gitlab_address),
        HealthCheck(
            "IA générative",
            HealthStatus.DISABLED,
            "désactivée (AI_PROVIDER=none)"
            if ai_provider == "none"
            else f"non intégrée dans cette version (AI_PROVIDER={ai_provider} ignoré)",
        ),
    ]
    if history is not None:
        checks.append(_history_check(history))
    if alerts is not None:
        checks.append(_alerts_check(alerts))
    return checks


@dataclass(frozen=True)
class AlertsSummary:
    channel_key: str | None  # ALERTS_CHANNEL
    channel_bound: bool  # salon associé par /setup
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
            "Alertes", HealthStatus.WARNING, f"salon « {summary.channel_key} » non associé : vérifiez ALERTS_CHANNEL puis /setup"
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
        return HealthCheck(name, HealthStatus.WARNING, "lancez /setup puis confirmez")
    if summary is None:
        return HealthCheck(name, HealthStatus.OK, "configuration confirmée")
    detail = f"{summary.published}/{summary.expected} publiés"
    if summary.provisional:
        detail += f", {summary.provisional} avec des liens à compléter"
    if summary.errors:
        return HealthCheck(name, HealthStatus.ERROR, f"{detail}, {summary.errors} en erreur : voir /admin messages")
    if summary.published < summary.expected:
        return HealthCheck(name, HealthStatus.WARNING, f"{detail} : lancez /refresh")
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


def _structure_check(structure: StructureReport | None) -> HealthCheck:
    if structure is None:
        return HealthCheck("Salons", HealthStatus.ERROR, "structure non vérifiée")
    total = len(structure.channels)
    usable = len(structure.bindings)
    if structure.is_complete:
        return HealthCheck("Salons", HealthStatus.OK, f"{usable}/{total} salons reconnus")
    return HealthCheck(
        "Salons", HealthStatus.WARNING, f"{usable}/{total} salons utilisables, détails avec /setup"
    )
