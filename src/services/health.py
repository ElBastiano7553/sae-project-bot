"""Rapport d'état utilisé par la commande /health."""

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
) -> list[HealthCheck]:
    """Construit la liste des vérifications à partir de valeurs déjà mesurées."""
    checks = [
        _discord_check(latency_ms),
        HealthCheck(
            "Base de données",
            HealthStatus.OK if database_ok else HealthStatus.ERROR,
            "SQLite répond" if database_ok else "SQLite ne répond pas : consultez logs/bot.log",
        ),
        HealthCheck(
            "Serveur Discord",
            HealthStatus.OK if guild_found else HealthStatus.ERROR,
            "serveur trouvé" if guild_found else "le bot n'est pas sur le serveur DISCORD_GUILD_ID",
        ),
        _structure_check(structure),
        HealthCheck(
            "Messages permanents",
            HealthStatus.OK if setup_confirmed else HealthStatus.WARNING,
            "configuration confirmée" if setup_confirmed else "lancez /setup puis confirmez",
        ),
        _gitlab_check(gitlab_enabled, gitlab_listening, gitlab_error, gitlab_last_event_at),
        HealthCheck(
            "IA générative",
            HealthStatus.DISABLED,
            "désactivée (AI_PROVIDER=none)" if ai_provider == "none" else f"« {ai_provider} » prévu en V4, non utilisé",
        ),
    ]
    return checks


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


def _gitlab_check(enabled: bool, listening: bool, error: str | None, last_event_at: str | None) -> HealthCheck:
    if not enabled:
        return HealthCheck("GitLab", HealthStatus.DISABLED, "désactivé (GITLAB_WEBHOOK_SECRET vide)")
    if error or not listening:
        return HealthCheck("GitLab", HealthStatus.ERROR, error or "serveur de webhooks arrêté")
    last = f"dernier événement : {last_event_at} UTC" if last_event_at else "aucun événement reçu pour l'instant"
    return HealthCheck("GitLab", HealthStatus.OK, f"webhooks actifs, {last}")


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
