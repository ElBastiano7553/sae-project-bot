"""Mise en forme des événements GitHub (fonctions pures, sans réseau)."""

import pytest

from src.github.events import (
    SUPPORTED_EVENTS,
    format_event,
    format_issue,
    format_pull_request,
    format_push,
    format_workflow_run,
    repository_full_name,
)
from src.github.samples import issue_payload, pull_request_payload, push_payload, workflow_run_payload
from src.gitlab.events import InvalidPayload

ZERO = "0" * 40


def test_supported_events():
    assert SUPPORTED_EVENTS == {"push", "pull_request", "issues", "workflow_run"}


def test_repository_full_name():
    assert repository_full_name(push_payload()) == "ElBastiano7553/game-project"
    assert repository_full_name({}) is None
    assert repository_full_name({"repository": "x"}) is None
    assert repository_full_name({"repository": {"full_name": 3}}) is None


# --- Push ------------------------------------------------------------------------


def test_push_lists_newest_commits_first():
    notification = format_push(push_payload())
    assert notification.kind == "github_push"
    assert notification.title == "📤 2 commits sur main · ElBastiano7553/game-project"
    first, second = notification.description.split("\n\n")[1].splitlines()
    assert "Corrige la collision" in first and "Ajoute le menu" in second
    assert notification.url.startswith("https://github.com/ElBastiano7553/game-project/compare/")
    assert notification.commit_count == 2
    fields = {field.name: field.value for field in notification.fields}
    assert fields["Fichiers"] == "+1 ajouté(s) · ~2 modifié(s) · −1 supprimé(s)"


def test_push_of_tag_is_ignored():
    payload = push_payload()
    payload["ref"] = "refs/tags/v1.0"
    assert format_push(payload) is None


def test_push_without_new_commits_is_ignored():
    payload = push_payload()
    for commit in payload["commits"]:
        commit["distinct"] = False  # avance rapide : commits déjà annoncés sur une autre branche
    assert format_push(payload) is None
    payload["commits"] = []
    assert format_push(payload) is None


def test_only_distinct_commits_are_counted():
    payload = push_payload()
    payload["commits"][0]["distinct"] = False
    notification = format_push(payload)
    assert notification.commit_count == 1
    assert "Ajoute le menu" not in notification.description


def test_branch_creation_and_deletion():
    created = push_payload()
    created.update(before=ZERO, created=True, commits=[])
    created["ref"] = "refs/heads/feature/score"
    notification = format_push(created)
    assert notification.title.startswith("🌱 Nouvelle branche : feature/score")
    assert notification.action == "branch_created"

    deleted = push_payload()
    deleted.update(after=ZERO, deleted=True, commits=[])
    notification = format_push(deleted)
    assert notification.title.startswith("🗑️ Branche supprimée : main")
    assert notification.action == "branch_deleted"


def test_forced_push_is_flagged():
    payload = push_payload()
    payload["forced"] = True
    notification = format_push(payload)
    assert "forcé un push" in notification.description
    assert any(field.name == "Attention" for field in notification.fields)


def test_many_commits_are_summarised():
    payload = push_payload()
    template = payload["commits"][0]
    payload["commits"] = [dict(template, id=f"{i:040x}", message=f"commit {i}") for i in range(9)]
    notification = format_push(payload)
    assert notification.title.startswith("📤 9 commits")
    assert "… et 4 autre(s) commit(s)" in notification.description


@pytest.mark.parametrize("mutate", [lambda p: p.pop("ref"), lambda p: p.update(commits="x"), lambda p: p.update(commits=[1]), lambda p: p.pop("repository")])
def test_invalid_push_payloads(mutate):
    payload = push_payload()
    mutate(payload)
    with pytest.raises(InvalidPayload):
        format_push(payload)


# --- Contenu non fiable ------------------------------------------------------------


def test_mentions_and_markdown_are_neutralised():
    payload = push_payload()
    payload["commits"][1]["message"] = "@everyone <@123> <@&456> **gras** [lien](https://evil.example)"
    payload["pusher"]["name"] = "@here"
    notification = format_push(payload)
    text = notification.description
    assert "@everyone" not in text and "@here" not in text
    assert "<@123>" not in text and "<@&456>" not in text
    assert "\\*\\*gras\\*\\*" in text
    assert "\\[lien\\]" in text


def test_pull_request_title_with_mentions_is_neutralised():
    payload = pull_request_payload()
    payload["pull_request"]["title"] = "@everyone regardez"
    payload["pull_request"]["body"] = "<@&999> merci @here"
    notification = format_pull_request(payload)
    assert "@everyone" not in notification.title
    assert "<@&999>" not in notification.description and "@here" not in notification.description


def test_unsafe_urls_are_dropped():
    payload = pull_request_payload()
    payload["pull_request"]["html_url"] = "javascript:alert(1)"
    notification = format_pull_request(payload)
    assert notification.url == "https://github.com/ElBastiano7553/game-project"


def test_very_long_text_is_truncated():
    payload = issue_payload()
    payload["issue"]["title"] = "x" * 5000
    payload["issue"]["body"] = "y" * 50_000
    notification = format_issue(payload)
    assert len(notification.title) <= 256
    assert len(notification.description) <= 300


# --- Pull requests -------------------------------------------------------------------


def test_pull_request_opened():
    notification = format_pull_request(pull_request_payload())
    assert notification.title == "🆕 PR #12 ouverte : Ajout du système de score · ElBastiano7553/game-project"
    fields = {field.name: field.value for field in notification.fields}
    assert fields["Branches"] == "`feature/score` → `main`"
    assert fields["Taille"] == "3 commit(s) · 5 fichier(s) · +120 / −14"
    assert fields["Relecteurs demandés"] == "alice"
    assert notification.description == "Ferme #3"


def test_pull_request_merged_and_closed():
    payload = pull_request_payload()
    payload["action"] = "closed"
    payload["pull_request"].update(merged=True, merged_at="2026-10-22T16:00:00Z", state="closed")
    merged = format_pull_request(payload)
    assert merged.title.startswith("🎉 PR #12 fusionnée")
    assert merged.action == "merge"
    assert any(field.name == "Fusionnée par" for field in merged.fields)

    payload["pull_request"]["merged"] = False
    closed = format_pull_request(payload)
    assert closed.title.startswith("🚫 PR #12 fermée")
    assert closed.dedup_key != merged.dedup_key


@pytest.mark.parametrize("action", ["synchronize", "edited", "labeled", "assigned", "review_requested", "merged", None])
def test_noisy_pull_request_actions_are_ignored(action):
    payload = pull_request_payload()
    payload["action"] = action
    assert format_pull_request(payload) is None


def test_draft_and_ready_for_review():
    payload = pull_request_payload()
    payload["pull_request"]["draft"] = True
    assert any(field.value == "brouillon (draft)" for field in format_pull_request(payload).fields)
    payload["action"] = "ready_for_review"
    payload["pull_request"]["draft"] = False
    assert "prête pour relecture" in format_pull_request(payload).title


def test_pull_request_without_number_is_invalid():
    payload = pull_request_payload()
    del payload["pull_request"]["number"]
    with pytest.raises(InvalidPayload):
        format_pull_request(payload)


# --- Issues -------------------------------------------------------------------------


def test_issue_opened_closed_reopened():
    payload = issue_payload()
    opened = format_issue(payload)
    assert opened.title == "🐛 Issue #7 ouverte : Le joueur traverse les murs · ElBastiano7553/game-project"
    assert {field.name: field.value for field in opened.fields}["Labels"] == "bug, niveau-2"

    payload["action"] = "closed"
    payload["issue"]["state_reason"] = "completed"
    assert format_issue(payload).title.startswith("✅ Issue #7 fermée")
    payload["issue"]["state_reason"] = "not_planned"
    assert "non prévue" in format_issue(payload).title
    payload["action"] = "reopened"
    assert format_issue(payload).title.startswith("🔁 Issue #7 rouverte")


@pytest.mark.parametrize("action", ["edited", "labeled", "assigned", "deleted", "transferred"])
def test_other_issue_actions_are_ignored(action):
    payload = issue_payload()
    payload["action"] = action
    assert format_issue(payload) is None


def test_issue_without_issue_object_is_invalid():
    payload = issue_payload()
    payload["issue"] = None
    with pytest.raises(InvalidPayload):
        format_issue(payload)


# --- GitHub Actions --------------------------------------------------------------------


def test_failed_workflow_run():
    notification = format_workflow_run(workflow_run_payload())
    assert notification.title == "❌ Workflow « Tests » en échec sur main · ElBastiano7553/game-project"
    fields = {field.name: field.value for field in notification.fields}
    assert fields["Branche"] == "`main`"
    assert fields["Origine"] == "un push"
    assert fields["Durée"]
    assert fields["Commit"].startswith("`da156088` Corrige la collision")
    assert notification.url.endswith("/actions/runs/777001")


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", "neutral", "action_required", None])
def test_non_failed_workflow_runs_are_ignored(conclusion):
    payload = workflow_run_payload()
    payload["workflow_run"]["conclusion"] = conclusion
    assert format_workflow_run(payload) is None


@pytest.mark.parametrize("action", ["requested", "in_progress"])
def test_unfinished_workflow_runs_are_ignored(action):
    payload = workflow_run_payload()
    payload["action"] = action
    assert format_workflow_run(payload) is None


@pytest.mark.parametrize("conclusion", ["timed_out", "startup_failure"])
def test_other_failures_are_notified(conclusion):
    payload = workflow_run_payload()
    payload["workflow_run"]["conclusion"] = conclusion
    assert format_workflow_run(payload) is not None


def test_rerun_of_a_failed_workflow_is_a_new_fact():
    first = format_workflow_run(workflow_run_payload())
    payload = workflow_run_payload()
    payload["workflow_run"]["run_attempt"] = 2
    second = format_workflow_run(payload)
    assert first.dedup_key != second.dedup_key
    assert {field.name: field.value for field in second.fields}["Tentative"] == "2"


def test_workflow_run_without_id_is_invalid():
    payload = workflow_run_payload()
    del payload["workflow_run"]["id"]
    with pytest.raises(InvalidPayload):
        format_workflow_run(payload)


def test_format_event_dispatches_by_header():
    assert format_event("issues", issue_payload()).kind == "github_issue"
    assert format_event("workflow_run", workflow_run_payload()).kind == "github_workflow"
