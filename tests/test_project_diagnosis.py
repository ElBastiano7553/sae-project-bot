"""/projet diagnostic : pourquoi un projet ne reçoit pas ses notifications (administrateurs, aucune modification)."""

from __future__ import annotations

import pytest

from src.database.project_deliveries import ProjectDeliveryRepository
from src.database.projects import ProjectRepository
from src.services.health import HealthStatus
from src.services.project_diagnosis import DeliveryFacts, ProjectFacts, ReceptionFacts, diagnose
from tests.fake_guild import FakeGuild
from tests.test_projects import (
    GITHUB_SECRET,
    GUILD,
    OTHER_GUILD,
    Interaction,
    Member,
    embed_text,
    make_bot,  # noqa: F401  (fixture)
)

APP_SECRET = "secret-github-app-de-test-0123456789"


class ListeningServer:
    listening = True

    async def stop(self) -> None:
        pass
INSTALLATION = 55

OPEN = ReceptionFacts(enabled=True, error=None, listening=True, github_open=True)
NOTHING = DeliveryFacts({}, None)


def facts(**changes) -> ProjectFacts:
    base = dict(provider="github", status="active", linked=True, installation_known=True, legacy_duplicate=False, routes=[("code", "")])
    return ProjectFacts(**{**base, **changes})


def statuses(diagnosis) -> list[HealthStatus]:
    return [check.status for check in diagnosis.checks]


# --- Règles (sans Discord) --------------------------------------------------------------------


def test_healthy_project_points_to_the_test_message():
    diagnosis = diagnose(facts(), OPEN, DeliveryFacts({"sent": 3}, "2026-10-24 10:00:00"))
    assert diagnosis.status is HealthStatus.OK
    assert "3 publiée(s)" in diagnosis.checks[3].detail
    assert "message de test" in diagnosis.hint


@pytest.mark.parametrize(
    ("reception", "expected"),
    [
        (ReceptionFacts(enabled=False, error=None, listening=False, github_open=False), "PROJECTS_HTTP_ENABLED"),
        (ReceptionFacts(enabled=True, error="port invalide", listening=False, github_open=True), "port invalide"),
        (ReceptionFacts(enabled=True, error=None, listening=False, github_open=True), "arrêtée"),
        (ReceptionFacts(enabled=True, error=None, listening=True, github_open=False), "GITHUB_APP_WEBHOOK_SECRET"),
    ],
)
def test_reception_problems_come_first(reception, expected):
    diagnosis = diagnose(facts(), reception, NOTHING)
    assert diagnosis.checks[0].status is HealthStatus.ERROR
    assert expected in diagnosis.checks[0].detail
    assert diagnosis.hint.startswith("À corriger d'abord : réception de samae")


def test_gitlab_project_does_not_need_the_github_secret():
    reception = ReceptionFacts(enabled=True, error=None, listening=True, github_open=False)
    assert diagnose(facts(provider="gitlab"), reception, NOTHING).checks[0].status is HealthStatus.OK


@pytest.mark.parametrize(
    ("changes", "status", "expected"),
    [
        ({"linked": False}, HealthStatus.ERROR, "/projet configurer"),
        ({"linked": False, "provider": "gitlab"}, HealthStatus.ERROR, "B5"),
        ({"status": "suspended"}, HealthStatus.ERROR, "ignorés"),
        ({"installation_known": False}, HealthStatus.WARNING, "plus vérifiée"),
        ({"legacy_duplicate": True}, HealthStatus.WARNING, "double"),
    ],
)
def test_connection_problems(changes, status, expected):
    check = diagnose(facts(**changes), OPEN, NOTHING).checks[1]
    assert check.status is status and expected in check.detail


def test_channel_problems_are_named_by_category():
    diagnosis = diagnose(facts(routes=[("code", ""), ("ci", "il lui manque « Envoyer des messages »")]), OPEN, NOTHING)
    assert diagnosis.checks[2].status is HealthStatus.ERROR
    assert "Intégration continue : il lui manque" in diagnosis.checks[2].detail


def test_no_channel_explains_that_events_are_not_even_recorded():
    check = diagnose(facts(routes=[]), OPEN, NOTHING).checks[2]
    assert check.status is HealthStatus.ERROR and "pas enregistrés" in check.detail


def test_nothing_received_from_github_points_to_recent_deliveries():
    diagnosis = diagnose(facts(), OPEN, NOTHING)
    assert statuses(diagnosis) == [HealthStatus.OK, HealthStatus.OK, HealthStatus.OK, HealthStatus.WARNING]
    assert "Recent deliveries" in diagnosis.hint and "Redeliver" in diagnosis.hint


def test_nothing_received_from_gitlab_points_to_the_webhook_page():
    diagnosis = diagnose(facts(provider="gitlab"), OPEN, NOTHING)
    assert "Webhooks" in diagnosis.hint and "désactive" in diagnosis.hint


def test_everything_filtered_points_to_the_filters():
    diagnosis = diagnose(facts(), OPEN, DeliveryFacts({"filtered": 4}, "2026-10-24 10:00:00"))
    assert "4 écartée(s) par les filtres" in diagnosis.checks[3].detail
    assert "filtres" in diagnosis.hint


def test_failed_sends_point_to_permissions():
    diagnosis = diagnose(facts(), OPEN, DeliveryFacts({"sent": 1, "failed": 2}, "2026-10-24 10:00:00"))
    assert diagnosis.checks[3].status is HealthStatus.WARNING
    assert "permissions" in diagnosis.hint


# --- Compteurs en base ------------------------------------------------------------------------


def test_summary_counts_only_this_project(db):
    deliveries = ProjectDeliveryRepository(db)
    projects = ProjectRepository(db)
    mine = projects.create(GUILD, "github", "equipe/a", "A", created_by=1)
    other = projects.create(OTHER_GUILD, "github", "equipe/a", "A", created_by=1)
    for key, status in (("k1", "sent"), ("k2", "sent"), ("k3", "filtered")):
        deliveries.claim(mine.id, GUILD, key, "code", dedup_key=None, channel_id=1, notification=None, status=status)
    deliveries.claim(other.id, OTHER_GUILD, "k9", "code", dedup_key=None, channel_id=2, notification=None, status="failed")
    counts, last = deliveries.project_summary(mine.id, 7)
    assert counts == {"sent": 2, "filtered": 1} and last is not None
    assert deliveries.project_summary(projects.create(GUILD, "github", "equipe/b", "B", created_by=1).id, 7) == ({}, None)


# --- Commande ---------------------------------------------------------------------------------


async def run(bot, guild, ref: str, user=None) -> dict:
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, guild, user or Member(1, admin=True))
    await cog.diagnostic.callback(cog, interaction, ref)
    return interaction.response.sent[0]


@pytest.fixture
async def connected(make_bot):  # noqa: F811
    bot = await make_bot(PROJECTS_HTTP_ENABLED="true", GITHUB_APP_WEBHOOK_SECRET=APP_SECRET)
    bot.projects_server = ListeningServer()
    guild = FakeGuild()
    channel = guild.add_channel("code")
    project = bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Jeu", created_by=1)
    bot.github_setup_repo.save_installation(GUILD, INSTALLATION, "equipe", ["equipe/jeu"], authorized_by=1)
    bot.projects_repo.link_github(GUILD, project.id, INSTALLATION)
    bot.projects_repo.set_route(GUILD, project.id, "code", channel.id)
    return bot, guild, channel, project


async def test_command_reports_a_connected_project_that_received_nothing(connected):
    bot, guild, _, project = connected
    sent = await run(bot, guild, str(project.id))
    text = embed_text(sent["embed"])
    assert sent["ephemeral"]
    assert "Diagnostic : Jeu" in text and "connecté" in text and "Recent deliveries" in text
    assert APP_SECRET not in text


async def test_command_sees_a_channel_where_samae_cannot_write(connected):
    bot, guild, _, project = connected
    guild.bot_permissions.discard("send_messages")
    text = embed_text((await run(bot, guild, str(project.id)))["embed"])
    assert "Envoyer des messages" in text and "À corriger d'abord : salons" in text


async def test_command_counts_deliveries_of_the_project(connected):
    bot, guild, channel, project = connected
    bot.project_deliveries_repo.claim(project.id, GUILD, "github:push:1", "code", dedup_key=None, channel_id=channel.id, notification=None, status="sent")
    text = embed_text((await run(bot, guild, str(project.id)))["embed"])
    assert "1 publiée(s)" in text


async def test_command_flags_a_removed_installation(connected):
    bot, guild, _, project = connected
    bot.database.conn.execute("DELETE FROM github_installations")
    text = embed_text((await run(bot, guild, str(project.id)))["embed"])
    assert "plus vérifiée" in text


async def test_command_flags_the_legacy_webhook_on_the_same_repository(make_bot):  # noqa: F811
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, GITHUB_WEBHOOK_REPOSITORY="equipe/jeu")
    project = bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Jeu", created_by=1)
    bot.github_setup_repo.save_installation(GUILD, INSTALLATION, "equipe", ["equipe/jeu"], authorized_by=1)
    bot.projects_repo.link_github(GUILD, project.id, INSTALLATION)
    text = embed_text((await run(bot, FakeGuild(), str(project.id)))["embed"])
    assert "double" in text
    assert "PROJECTS_HTTP_ENABLED" in text  # réception désactivée : c'est elle qu'il faut corriger d'abord


@pytest.mark.parametrize("ref", ["999", "env-github", "abc"])
async def test_unknown_or_legacy_reference_is_refused(connected, ref):
    bot, guild, _, _ = connected
    sent = await run(bot, guild, ref)
    assert "introuvable" in embed_text(sent["embed"])


async def test_another_guilds_project_is_refused(connected):
    bot, guild, _, _ = connected
    foreign = bot.projects_repo.create(OTHER_GUILD, "github", "autre/depot", "Autre", created_by=1)
    sent = await run(bot, guild, str(foreign.id))
    assert "introuvable" in embed_text(sent["embed"])


async def test_command_changes_nothing(connected):
    bot, guild, _, project = connected
    before = bot.database.conn.execute("SELECT total_changes()").fetchone()[0]
    await run(bot, guild, str(project.id))
    assert bot.database.conn.execute("SELECT total_changes()").fetchone()[0] == before
    assert [a.action for a in bot.admin_log.recent()] == []
