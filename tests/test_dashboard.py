"""Tableau de bord : contenu calculé uniquement depuis les événements enregistrés."""

import datetime as dt

from src.database import EventSummary, GitLabEventRepository
from src.services.content import load_message_templates, render_message
from src.services.dashboard import DASHBOARD_KEY, DashboardData, collect_dashboard, dashboard_fields
from tests.test_content import EMPTY_PLANNING, TODAY

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


def claim(repo, key, kind, at, **summary):
    repo.claim(key, kind, summary=EventSummary(dedup_key=key, occurred_at=at, **summary))


def names(fields):
    return {f.name: f.value for f in fields}


def test_disabled_gitlab_says_so_and_invents_nothing():
    fields = dashboard_fields(DashboardData(gitlab_enabled=False))
    assert len(fields) == 1 and "désactivés" in fields[0].name


def test_no_event_yet():
    fields = dashboard_fields(DashboardData(gitlab_enabled=True))
    assert "Aucun événement" in fields[0].value


def test_dashboard_from_recorded_events(db):
    repo = GitLabEventRepository(db)
    claim(repo, "mr1", "merge_request", "2026-10-21T10:00:00Z", action="open", title="MR ouverte", subject_id=1)
    claim(repo, "mr2a", "merge_request", "2026-10-20T10:00:00Z", action="open", title="MR fusionnée", subject_id=2)
    claim(repo, "mr2b", "merge_request", "2026-10-21T11:00:00Z", action="merge", title="MR fusionnée", subject_id=2)
    claim(repo, "pl1", "pipeline", "2026-10-21T09:00:00Z", action="failed", ref="main", subject_id=10, title="pipeline main")
    claim(repo, "pl2", "pipeline", "2026-10-21T09:30:00Z", action="success", ref="main", subject_id=11)
    claim(repo, "pl3", "pipeline", "2026-10-21T09:40:00Z", action="failed", ref="feature/x", subject_id=12, title="pipeline x")
    claim(repo, "p1", "push", "2026-10-21T08:00:00Z", action="push", commit_count=3, ref="main")
    claim(repo, "d1", "deployment", "2026-10-21T12:00:00Z", action="success", environment="production", ref="main", subject_id=5)

    data = collect_dashboard(repo, default_branch="main", gitlab_enabled=True, now=NOW)
    assert [r.subject_id for r in data.open_mrs] == [1]
    assert [r.ref for r in data.failing_branches] == ["feature/x"]  # main a été réparée depuis
    assert data.week.commits == 3 and data.week.mr_merged == 1
    assert data.previous_week is None  # l'historique ne couvre pas la semaine précédente

    fields = names(dashboard_fields(data))
    assert "réussi" in fields["🌿 Branche principale"]
    assert "MR ouverte" in fields["🔀 Merge requests ouvertes (1)"]
    assert "feature/x" in fields["❌ Branches dont le dernier pipeline a échoué"]
    assert "production" in fields["🚀 Derniers déploiements"]
    assert "3 commits" in fields["📈 7 derniers jours"]
    assert "8 événements" in fields["ℹ️ Source"]


def test_rendering_is_stable_when_nothing_changes(db):
    repo = GitLabEventRepository(db)
    claim(repo, "p1", "push", "2026-10-21T08:00:00Z", action="push", commit_count=1)
    template = next(t for t in load_message_templates() if t.key == DASHBOARD_KEY)
    assert template.channel_key == "gitlab_activite"

    def render():
        data = collect_dashboard(repo, default_branch=None, gitlab_enabled=True, now=NOW)
        return render_message(template, {"gitlab_project": "", "gitlab_merge_requests": ""}, EMPTY_PLANNING, TODAY,
                              dashboard=dashboard_fields(data))

    # Même données, même contenu : le message n'est pas modifié inutilement sur Discord.
    assert render().content_hash() == render().content_hash()


def test_template_without_data_shows_placeholder():
    template = next(t for t in load_message_templates() if t.key == DASHBOARD_KEY)
    payload = render_message(template, {"gitlab_project": "", "gitlab_merge_requests": ""}, EMPTY_PLANNING, TODAY)
    assert any("pas encore calculé" in f.value for f in payload.fields)
