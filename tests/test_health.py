from src.services.health import HealthStatus, build_health_checks, overall_status
from src.services.server_structure import StructureReport


def checks(**overrides):
    params = dict(
        latency_ms=42.0,
        database_ok=True,
        guild_found=True,
        structure=StructureReport(),
        setup_confirmed=True,
        ai_provider="none",
    )
    return {c.name: c for c in build_health_checks(**{**params, **overrides})}


def test_everything_ok_without_gitlab_or_ai():
    result = checks()
    assert result["IA générative"].status is HealthStatus.DISABLED
    assert result["GitLab"].status is HealthStatus.DISABLED
    assert overall_status(list(result.values())) is HealthStatus.OK


def test_missing_ai_key_is_not_an_error():
    result = checks(ai_provider="none")
    assert "désactivée" in result["IA générative"].detail


def test_database_failure_is_an_error():
    result = checks(database_ok=False)
    assert overall_status(list(result.values())) is HealthStatus.ERROR


def test_setup_not_done_is_a_warning():
    result = checks(setup_confirmed=False)
    assert result["Messages permanents"].status is HealthStatus.WARNING
    assert overall_status(list(result.values())) is HealthStatus.WARNING


def test_unknown_latency():
    assert checks(latency_ms=float("nan"))["Connexion Discord"].status is HealthStatus.WARNING


def test_guild_not_found():
    assert checks(guild_found=False)["Serveur principal"].status is HealthStatus.ERROR


def test_no_home_guild_is_not_an_error():
    result = checks(guild_found=None)
    assert result["Serveur principal"].status is HealthStatus.DISABLED
    assert overall_status(list(result.values())) is not HealthStatus.ERROR


def test_gitlab_disabled_is_not_an_error():
    result = checks(gitlab_enabled=False)
    assert result["GitLab"].status is HealthStatus.DISABLED
    assert "GITLAB_WEBHOOK_SECRET" in result["GitLab"].detail


def test_gitlab_listening():
    result = checks(gitlab_enabled=True, gitlab_listening=True, gitlab_last_event_at="2026-10-22 15:00:00")
    assert result["GitLab"].status is HealthStatus.OK
    assert "2026-10-22 15:00:00" in result["GitLab"].detail


def test_gitlab_server_failure_is_an_error():
    result = checks(gitlab_enabled=True, gitlab_listening=False, gitlab_error="port 8080 déjà utilisé")
    assert result["GitLab"].status is HealthStatus.ERROR
    assert "8080" in result["GitLab"].detail


def test_alerts_and_history_lines():
    from src.services.health import AlertsSummary, HistorySummary

    off = checks(alerts=AlertsSummary(None, False, 2))["Alertes"]
    assert off.status is HealthStatus.DISABLED and "ALERTS_CHANNEL" in off.detail
    unbound = checks(alerts=AlertsSummary("bilan_ia", False, 0))["Alertes"]
    assert unbound.status is HealthStatus.WARNING
    assert checks(alerts=AlertsSummary("bilan_ia", True, 1))["Alertes"].status is HealthStatus.OK
    history = checks(history=HistorySummary(365, 12, "2026-10-01 10:00:00", 2))["Historique"]
    assert "12 événement(s) depuis le 2026-10-01, dont 2 déploiement(s)" in history.detail
    assert "aucun événement" in checks(history=HistorySummary(365, 0, None, 0))["Historique"].detail
