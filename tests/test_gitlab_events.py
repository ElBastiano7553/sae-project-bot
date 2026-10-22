import pytest

from src.gitlab.events import ZERO_SHA, InvalidPayload, format_event, format_merge_request, format_pipeline, format_push, project_id_of
from src.gitlab.rules import GitLabRules
from src.gitlab.samples import merge_request_payload, pipeline_payload, push_payload

RULES = GitLabRules(routes={"push": "a", "merge_request": "a", "pipeline": "b"})


# --- Push --------------------------------------------------------------------


def test_push_lists_latest_commits_first_with_links():
    n = format_push(push_payload(), RULES)
    assert n.title == "📤 2 commits sur main · epita/sae-game"
    assert n.url == "https://gitlab.com/epita/sae-game/-/compare/95790bf891e7...da1560886d4f"
    assert n.description.index("Corrige la collision") < n.description.index("Ajoute le menu")
    assert "[`da156088`](https://gitlab.com/epita/sae-game/-/commit/da1560886d4f094c3e6c9ef40349f7d38b5d27d7)" in n.description
    assert "Détails du commit" not in n.description  # seule la première ligne est affichée
    assert n.footer == "GitLab · epita/sae-game"


def test_push_limits_number_of_commits():
    payload = push_payload(total_commits_count=30)
    rules = GitLabRules(routes=RULES.routes, max_commits=1)
    n = format_push(payload, rules)
    assert "Ajoute le menu" not in n.description
    assert "… et 29 autre(s) commit(s)" in n.description
    assert n.title.startswith("📤 30 commits")


def test_push_single_commit_is_singular():
    payload = push_payload(total_commits_count=1)
    payload["commits"] = payload["commits"][:1]
    assert format_push(payload, RULES).title.startswith("📤 1 commit sur")


def test_new_branch_without_commits():
    n = format_push(push_payload(before=ZERO_SHA, commits=[], total_commits_count=0, ref="refs/heads/feature/x"), RULES)
    assert n.title.startswith("🌱 Nouvelle branche : feature/x")
    assert n.url.endswith("/-/tree/da1560886d4f")


def test_deleted_branch():
    n = format_push(push_payload(after=ZERO_SHA, commits=[], total_commits_count=0), RULES)
    assert n.title.startswith("🗑️ Branche supprimée : main")


def test_push_without_new_commit_is_ignored():
    assert format_push(push_payload(commits=[], total_commits_count=0), RULES) is None


def test_push_on_unfollowed_branch_is_ignored():
    rules = GitLabRules(routes=RULES.routes, push_branches=("main",))
    assert format_push(push_payload(ref="refs/heads/test"), rules) is None


def test_tag_ref_is_ignored():
    assert format_push(push_payload(ref="refs/tags/v1.0"), RULES) is None


def test_commit_message_cannot_ping_or_inject_links():
    payload = push_payload()
    payload["commits"][1]["title"] = "@everyone [gagnez](https://evil.example)"
    payload["commits"][1]["url"] = "javascript:alert(1)"
    n = format_push(payload, RULES)
    assert "@everyone" not in n.description
    assert "[gagnez](" not in n.description
    assert "javascript" not in n.description


def test_push_respects_discord_limits():
    payload = push_payload(total_commits_count=10)
    payload["commits"] = [{**payload["commits"][0], "title": "x" * 5000} for _ in range(10)]
    payload["ref"] = "refs/heads/" + "b" * 500
    n = format_push(payload, GitLabRules(routes=RULES.routes, max_commits=10))
    assert len(n.title) <= 256
    assert len(n.description) <= 4096


@pytest.mark.parametrize(
    "payload",
    [
        {"object_kind": "push"},
        push_payload(ref=None),
        push_payload(project=None),
        push_payload(commits="pas une liste"),
        push_payload(object_kind="pipeline"),
    ],
)
def test_push_invalid_payload(payload):
    with pytest.raises(InvalidPayload):
        format_push(payload, RULES)


# --- Merge Requests ------------------------------------------------------------


def test_merge_request_opened():
    n = format_merge_request(merge_request_payload(), RULES)
    assert n.title == "🆕 MR !12 ouverte : Ajout du système de score"
    assert n.url == "https://gitlab.com/epita/sae-game/-/merge_requests/12"
    fields = {f.name: f.value for f in n.fields}
    assert fields["Branches"] == "`feature/score` → `main`"
    assert fields["Par"] == "Bob Durand"
    assert "Ferme #3" in n.description


@pytest.mark.parametrize("action, label", [("merge", "fusionnée"), ("close", "fermée"), ("approved", "approuvée"), ("reopen", "rouverte")])
def test_merge_request_actions(action, label):
    assert label in format_merge_request(merge_request_payload(action=action), RULES).title


def test_merge_request_update_lists_changed_fields():
    payload = merge_request_payload(action="update")
    payload["changes"] = {"title": {"previous": "a", "current": "b"}, "labels": {}, "updated_at": {}}
    n = format_merge_request(payload, RULES)
    assert n.title.startswith("✏️ MR !12 mise à jour")
    assert {f.name: f.value for f in n.fields}["Modifié"] == "titre, labels"


def test_merge_request_update_caused_by_new_commits_is_ignored():
    payload = merge_request_payload(action="update", oldrev="95790bf891e76fee5e1747ab589903a6a1f80f22")
    payload["changes"] = {"updated_at": {}}
    assert format_merge_request(payload, RULES) is None


def test_merge_request_technical_update_is_ignored():
    payload = merge_request_payload(action="update")
    payload["changes"] = {"merge_status": {}, "updated_at": {}}
    assert format_merge_request(payload, RULES) is None


def test_merge_request_update_can_be_disabled():
    rules = GitLabRules(routes=RULES.routes, mr_actions=frozenset({"open"}))
    payload = merge_request_payload(action="update")
    payload["changes"] = {"title": {}}
    assert format_merge_request(payload, rules) is None


def test_merge_request_draft_is_shown():
    fields = {f.name: f.value for f in format_merge_request(merge_request_payload(draft=True), RULES).fields}
    assert fields["État"] == "brouillon (draft)"


def test_merge_request_without_iid_is_invalid():
    with pytest.raises(InvalidPayload):
        format_merge_request(merge_request_payload(iid=None), RULES)


def test_merge_request_without_attributes_is_invalid():
    with pytest.raises(InvalidPayload):
        format_merge_request({"object_kind": "merge_request", "project": {}}, RULES)


# --- Pipelines -----------------------------------------------------------------


def test_failed_pipeline_lists_blocking_failed_jobs_only():
    n = format_pipeline(pipeline_payload(), RULES)
    assert n.title == "❌ Pipeline #31 en échec · epita/sae-game"
    fields = {f.name: f.value for f in n.fields}
    assert fields["Branche"] == "`main`"
    assert fields["Durée"] == "2 min 05 s"
    assert "pytest (test)" in fields["Jobs en échec"]
    assert "ruff" not in fields["Jobs en échec"]  # allow_failure : n'empêche pas le pipeline
    assert n.url == "https://gitlab.com/epita/sae-game/-/pipelines/31"


def test_successful_pipeline():
    payload = pipeline_payload(status="success")
    payload["builds"] = []
    n = format_pipeline(payload, RULES)
    assert n.title.startswith("✅ Pipeline #31 réussi")
    assert "Jobs en échec" not in {f.name for f in n.fields}


@pytest.mark.parametrize("status", ["running", "pending", "created"])
def test_intermediate_pipeline_statuses_are_ignored(status):
    assert format_pipeline(pipeline_payload(status=status), RULES) is None


def test_pipeline_branch_filter():
    rules = GitLabRules(routes=RULES.routes, pipeline_branches=("main",))
    assert format_pipeline(pipeline_payload(ref="feature/x"), rules) is None
    assert format_pipeline(pipeline_payload(ref="main"), rules) is not None


def test_pipeline_url_fallback():
    n = format_pipeline(pipeline_payload(url=None), RULES)
    assert n.url == "https://gitlab.com/epita/sae-game/-/pipelines/31"


def test_pipeline_without_id_is_invalid():
    with pytest.raises(InvalidPayload):
        format_pipeline(pipeline_payload(id="31"), RULES)


# --- Divers --------------------------------------------------------------------


def test_format_event_dispatches_by_kind():
    assert format_event("pipeline", pipeline_payload(), RULES).kind == "pipeline"
    assert format_event("push", push_payload(), RULES).kind == "push"


def test_project_id_of():
    assert project_id_of(push_payload()) == 4242
    assert project_id_of({"project_id": 7}) == 7
    assert project_id_of({"project": {"id": "7"}}) is None


# --- Lot 1 : contenu enrichi et empreintes anti-doublon ---------------------------


def test_push_shows_author_files_and_event_time():
    n = format_push(push_payload(), RULES)
    fields = {f.name: f.value for f in n.fields}
    assert fields["Auteur"] == "Alice Martin"
    # Fichiers distincts sur l'ensemble des commits : main.py modifié deux fois ne compte qu'une fois.
    assert fields["Fichiers"] == "+1 ajouté(s) · ~2 modifié(s) · −1 supprimé(s)"
    assert n.timestamp == "2026-10-22T13:04:00+00:00"  # dernier commit, converti en UTC
    assert (n.action, n.ref, n.default_branch) == ("push", "main", "main")


def test_push_without_file_lists_shows_no_fake_statistics():
    payload = push_payload()
    for commit in payload["commits"]:
        for key in ("added", "modified", "removed"):
            commit.pop(key)
    assert "Fichiers" not in {f.name for f in format_push(payload, RULES).fields}


def test_push_file_stats_mention_partial_coverage():
    n = format_push(push_payload(total_commits_count=40), RULES)
    assert "d'après les 2 commits détaillés" in {f.name: f.value for f in n.fields}["Fichiers"]


def test_same_fact_gives_same_dedup_key_and_different_facts_differ():
    assert format_push(push_payload(), RULES).dedup_key == format_push(push_payload(), RULES).dedup_key
    assert format_push(push_payload(), RULES).dedup_key != format_push(push_payload(after="1" * 40), RULES).dedup_key
    first = format_pipeline(pipeline_payload(), RULES).dedup_key
    assert first == format_pipeline(pipeline_payload(), RULES).dedup_key
    assert first != format_pipeline(pipeline_payload(status="success"), RULES).dedup_key
    # La même MR refermée plus tard (autre date de mise à jour) est un nouvel événement.
    closed = format_merge_request(merge_request_payload(action="close"), RULES).dedup_key
    reclosed = format_merge_request(merge_request_payload(action="close", updated_at="2026-10-23 08:00:00 UTC"), RULES)
    assert closed != reclosed.dedup_key


def test_merge_request_without_update_date_is_not_deduplicated_by_content():
    assert format_merge_request(merge_request_payload(updated_at=None), RULES).dedup_key is None


def test_merge_request_shows_reviewers_assignees_and_status():
    fields = {f.name: f.value for f in format_merge_request(merge_request_payload(), RULES).fields}
    assert fields["Relecteurs"] == "Alice Martin"
    assert fields["Assignés"] == "Bob Durand"
    assert fields["Statut"] == "ouverte · approbation requise"


def test_merge_request_approval_names_the_approver():
    fields = {f.name: f.value for f in format_merge_request(merge_request_payload(action="approved"), RULES).fields}
    assert fields["Approuvée par"] == "Bob Durand"


def test_merged_request_has_no_redundant_status():
    n = format_merge_request(merge_request_payload(action="merge", state="merged"), RULES)
    assert "Statut" not in {f.name for f in n.fields}
    assert n.color == 0x9B59B6


def test_merge_request_people_cannot_ping():
    payload = merge_request_payload()
    payload["reviewers"] = [{"name": "@everyone"}]
    assert "@everyone" not in {f.name: f.value for f in format_merge_request(payload, RULES).fields}["Relecteurs"]


def test_pipeline_links_commit_and_shows_source_and_time():
    n = format_pipeline(pipeline_payload(), RULES)
    fields = {f.name: f.value for f in n.fields}
    assert "[`da156088`](https://gitlab.com/epita/sae-game/-/commit/da1560886d4f094c3e6c9ef40349f7d38b5d27d7)" in fields["Commit"]
    assert fields["Déclenché par"] == "un push"
    assert n.timestamp == "2026-10-22T13:07:05+00:00"
    assert n.action == "failed"


def test_pipeline_success_never_claims_a_deployment():
    n = format_pipeline(pipeline_payload(status="success"), RULES)
    text = " ".join([n.title, n.description, *[f.name + f.value for f in n.fields]]).lower()
    assert "déploi" not in text


def test_unknown_pipeline_status_is_shown_as_received():
    rules = GitLabRules(routes=RULES.routes, pipeline_statuses=frozenset({"scheduled"}))
    n = format_pipeline(pipeline_payload(status="scheduled"), rules)
    assert "scheduled" in n.title
