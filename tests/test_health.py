from src.services.health import HealthStatus, build_health_checks, overall_status
from src.services.server_structure import StructureReport


def checks(**overrides):
    params = dict(
        latency_ms=42.0,
        database_ok=True,
        guild_found=True,
        structure=StructureReport(),
        setup_confirmed=True,
        gitlab_configured=False,
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
    assert checks(guild_found=False)["Serveur Discord"].status is HealthStatus.ERROR
