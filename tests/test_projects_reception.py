"""Multi-projets, étape B3 : réception commune des événements des projets enregistrés.

Vraies requêtes HTTP sur un port local temporaire, avec des exemples d'événements GitHub et GitLab.
Les liens avec les fournisseurs (créés par B4 et B5) sont posés directement en base.
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.config import Settings
from src.database import Database
from src.database.connection import MIGRATIONS
from src.database.project_deliveries import ProjectDeliveryRepository
from src.database.projects import ProjectRepository
from src.github import samples as gh
from src.github.security import compute_signature
from src.github.webhook import WEBHOOK_PATH as LEGACY_GITHUB_PATH
from src.gitlab import samples as gl
from src.gitlab.dispatcher import EventStatus
from src.gitlab.webhook import WEBHOOK_PATH as LEGACY_GITLAB_PATH
from src.providers.dispatcher import ProjectDispatcher
from src.providers.server import GITHUB_PATH, MAX_BODY_BYTES, create_projects_app
from src.providers.translate import translate_github, translate_gitlab
from src.services.health import ProjectsReceptionSummary, _projects_check
from src.services.projects import Route, hash_secret, registered_view, route_allows, secret_matches
from tests.test_projects import GUILD, OTHER_GUILD

APP_SECRET = "secret-github-app-de-test-0123456789"
HOOK = "hook-projet-outils-0001"
HOOK_SECRET = "secret-gitlab-projet-0123456789"
INSTALLATION = 55
REPO = "ElBastiano7553/game-project"
CODE_CHANNEL, CI_CHANNEL, OTHER_CHANNEL = 101, 102, 901


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, object]] = []

    async def send_notification(self, channel_id, notification):
        self.sent.append((channel_id, notification))


@pytest.fixture
def projects(db):
    return ProjectRepository(db)


@pytest.fixture
def deliveries(db):
    return ProjectDeliveryRepository(db)


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def channels():
    """Salon → serveur Discord, comme Discord le dirait au moment de l'envoi."""
    return {CODE_CHANNEL: GUILD, CI_CHANNEL: GUILD, OTHER_CHANNEL: OTHER_GUILD}


@pytest.fixture
def dispatcher(projects, deliveries, sender, channels):
    async def channel_guild(channel_id):
        return channels.get(channel_id)

    return ProjectDispatcher(projects, deliveries, sender, channel_guild, retry_delays=())


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
async def client(projects, dispatcher):
    client = await _client(create_projects_app(github_secret=APP_SECRET, projects=projects, dispatcher=dispatcher))
    yield client
    await client.close()


@pytest.fixture
def game(projects):
    """Projet GitHub du serveur GUILD, relié à l'installation de l'App, code et CI publiés."""
    project = projects.create(GUILD, "github", REPO, "Jeu", created_by=1)
    projects.set_route(GUILD, project.id, "code", CODE_CHANNEL)
    projects.set_route(GUILD, project.id, "ci", CI_CHANNEL)
    projects.link_github(GUILD, project.id, INSTALLATION)
    return project


@pytest.fixture
def tools(projects):
    """Projet GitLab n° 4242 du serveur GUILD, relié au webhook HOOK, revues publiées."""
    project = projects.create(GUILD, "gitlab", "4242", "Outils", created_by=1)
    projects.set_route(GUILD, project.id, "revues", CODE_CHANNEL)
    projects.set_route(GUILD, project.id, "ci", CI_CHANNEL)
    projects.link_gitlab(GUILD, project.id, HOOK, hash_secret(HOOK_SECRET))
    return project


def app_payload(builder, installation=INSTALLATION, **changes) -> dict:
    payload = builder()
    payload.update(changes)
    if installation is not None:
        payload["installation"] = {"id": installation}
    return payload


async def post_github(client, payload, *, event="push", delivery=None, secret=APP_SECRET, signature=None, content_type="application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": content_type, "X-GitHub-Event": event, "X-GitHub-Delivery": delivery or str(uuid.uuid4())}
    if signature is not False:
        headers["X-Hub-Signature-256"] = signature or compute_signature(secret, body)
    response = await client.post(GITHUB_PATH, data=body, headers=headers)
    return response.status, await response.json()


async def post_gitlab(client, payload, *, event="Merge Request Hook", hook=HOOK, token=HOOK_SECRET, uuid_header=None):
    headers = {"Content-Type": "application/json", "X-Gitlab-Event": event, "X-Gitlab-Event-UUID": uuid_header or str(uuid.uuid4())}
    if token is not None:
        headers["X-Gitlab-Token"] = token
    response = await client.post(f"/gitlab/webhook/{hook}", data=json.dumps(payload).encode("utf-8"), headers=headers)
    return response.status, await response.json()


# --- Migration v10 et liens ---------------------------------------------------------------------


def test_v9_database_is_upgraded_to_v10_without_losing_projects(tmp_path):
    path = tmp_path / "v9.sqlite3"
    conn = sqlite3.connect(path)
    for version, script in enumerate(MIGRATIONS[:9], start=1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("INSERT INTO projects (guild_id, provider, external_id, name, created_by) VALUES (42, 'github', 'a/b', 'Jeu', 1)")
    conn.execute("INSERT INTO project_routes (project_id, category, channel_id) VALUES (1, 'code', 5)")
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db.schema_version == len(MIGRATIONS) >= 10
        repo = ProjectRepository(db)
        assert repo.routes(42, 1) == (Route("code", 5, {}),)
        assert repo.link(42, 1) is None  # aucun projet existant n'est relié par la migration
        assert db.conn.execute("SELECT COUNT(*) FROM project_deliveries").fetchone()[0] == 0
    finally:
        db.close()
    assert len(list((tmp_path / "backups").glob(f"*-avantv{len(MIGRATIONS)}.sqlite3"))) == 1


def test_link_table_refuses_inconsistent_rows(db, projects):
    project = projects.create(GUILD, "github", "a/b", "Jeu", created_by=1)
    for values in (("github", None, None, None), ("gitlab", None, "x" * 16, "trop-court"), ("github", 1, "x" * 16, None)):
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute("INSERT INTO provider_links (project_id, provider, installation_id, hook_id, secret_hash) VALUES (?, ?, ?, ?, ?)", (project.id, *values))


def test_links_are_guild_scoped_and_checked(projects):
    mine = projects.create(GUILD, "github", "a/b", "Jeu", created_by=1)
    theirs = projects.create(OTHER_GUILD, "gitlab", "12", "Leur projet", created_by=9)
    with pytest.raises(Exception, match="introuvable"):
        projects.link_gitlab(GUILD, theirs.id, HOOK, hash_secret(HOOK_SECRET))  # projet d'un autre serveur
    with pytest.raises(Exception, match="introuvable"):
        projects.link_gitlab(GUILD, mine.id, HOOK, hash_secret(HOOK_SECRET))  # mauvais fournisseur
    with pytest.raises(Exception, match="invalide"):
        projects.link_github(GUILD, mine.id, 0)
    projects.link_gitlab(OTHER_GUILD, theirs.id, HOOK, hash_secret(HOOK_SECRET))
    assert projects.link(GUILD, theirs.id) is None and projects.unlink(GUILD, theirs.id) is False
    assert projects.link(OTHER_GUILD, theirs.id).hook_id == HOOK
    other = projects.create(GUILD, "gitlab", "13", "Autre", created_by=1)
    with pytest.raises(Exception, match="déjà utilisé"):
        projects.link_gitlab(GUILD, other.id, HOOK, hash_secret("autre-secret-0123456789"))


def test_secret_is_stored_only_as_a_hash(db, tools):
    dump = "\n".join(db.conn.iterdump())
    assert HOOK_SECRET not in dump and hash_secret(HOOK_SECRET) in dump
    assert secret_matches(hash_secret(HOOK_SECRET), HOOK_SECRET)
    assert not secret_matches(hash_secret(HOOK_SECRET), HOOK_SECRET + "x")
    assert not secret_matches(hash_secret(HOOK_SECRET), None) and not secret_matches("", HOOK_SECRET)


def test_deleting_a_project_removes_its_link_and_deliveries(db, projects, deliveries, game):
    deliveries.claim(game.id, GUILD, "github:push:1", "code", dedup_key="d", channel_id=CODE_CHANNEL, notification="{}")
    assert projects.delete(GUILD, game.id)
    assert db.conn.execute("SELECT COUNT(*) FROM provider_links").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM project_deliveries").fetchone()[0] == 0


def test_status_is_connected_only_with_a_link(projects, game):
    pending = projects.create(GUILD, "github", "a/b", "Sans lien", created_by=1)
    assert registered_view(pending, ()).status == "pending"
    assert registered_view(game, (), connected=True).status_label == "connecté : notifications actives"


def test_github_targets_need_the_installation_the_repository_and_an_active_project(db, projects, game):
    assert [p.id for p in projects.github_targets(INSTALLATION, REPO.upper())] == [game.id]
    assert projects.github_targets(INSTALLATION + 1, REPO) == []  # autre installation : quelqu'un d'autre
    assert projects.github_targets(INSTALLATION, "autre/depot") == []
    projects.create(OTHER_GUILD, "github", REPO, "Même dépôt, sans lien", created_by=9)
    assert [p.id for p in projects.github_targets(INSTALLATION, REPO)] == [game.id]
    db.conn.execute("UPDATE projects SET status = 'suspended' WHERE id = ?", (game.id,))
    assert projects.github_targets(INSTALLATION, REPO) == []


# --- Traduction et filtres (sans HTTP) -----------------------------------------------------------


def test_github_events_are_translated_into_the_common_vocabulary():
    push = translate_github("push", "1", gh.push_payload())
    assert (push.category, push.value, push.branch) == ("code", None, "main")
    pr = gh.pull_request_payload()
    pr["action"] = "closed"
    pr["pull_request"]["merged"] = True
    assert translate_github("pull_request", "2", pr).value == "fusion"
    issue = gh.issue_payload()
    issue["action"] = "closed"
    assert (translate_github("issues", "3", issue).category, translate_github("issues", "3", issue).value) == ("tickets", "fermeture")
    run = translate_github("workflow_run", "4", gh.workflow_run_payload())
    assert (run.category, run.value) == ("ci", "echoue")
    assert translate_github("star", "5", {}) is None


def test_gitlab_events_are_translated_into_the_common_vocabulary():
    assert translate_gitlab("merge_request", "1", gl.merge_request_payload(action="approved")).value == "approbation"
    pipeline = translate_gitlab("pipeline", "2", gl.pipeline_payload(status="success"))
    assert (pipeline.category, pipeline.value, pipeline.branch) == ("ci", "reussi", "main")
    assert translate_gitlab("deployment", "3", gl.deployment_payload(status="running")).value == "en_cours"
    assert translate_gitlab("pipeline", "4", gl.pipeline_payload(status="skipped")) is None


@pytest.mark.parametrize(
    ("category", "filters", "value", "branch", "allowed"),
    [
        ("code", {}, None, "dev", True),
        ("code", {"branches": ["main", "feature/*"]}, None, "feature/menu", True),
        ("code", {"branches": ["main"]}, None, "dev", False),
        ("ci", {"branches": ["main"]}, "echoue", None, False),
        ("ci", {"statuts": ["echoue"]}, "reussi", "main", False),
        ("ci", {"statuts": ["echoue"], "branches": ["main"]}, "echoue", "main", True),
        ("revues", {"actions": ["fusion"]}, "fusion", None, True),
        ("revues", {"actions": ["fusion"]}, None, None, False),  # action sans équivalent commun
        ("revues", {"branches": ["main"]}, "ouverture", None, True),  # pas de filtre de branche pour les revues
    ],
)
def test_route_filters(category, filters, value, branch, allowed):
    assert route_allows(category, filters, value=value, branch=branch) is allowed


# --- GitHub App : accès autorisés ----------------------------------------------------------------


async def test_linked_project_receives_its_event_in_its_channel(client, dispatcher, sender, deliveries, game):
    status, body = await post_github(client, app_payload(gh.push_payload), delivery="d-1")
    assert (status, body["status"]) == (200, "queued")
    await dispatcher.drain()
    [(channel, notification)] = sender.sent
    assert channel == CODE_CHANNEL and notification.kind == "github_push"
    assert deliveries.get_status(game.id, "github:push:d-1") == EventStatus.SENT


async def test_same_repository_in_two_servers_gets_two_isolated_notifications(client, dispatcher, sender, projects, game, channels):
    theirs = projects.create(OTHER_GUILD, "github", REPO, "Leur copie", created_by=9)
    projects.set_route(OTHER_GUILD, theirs.id, "code", OTHER_CHANNEL)
    projects.link_github(OTHER_GUILD, theirs.id, INSTALLATION)
    await post_github(client, app_payload(gh.push_payload))
    await dispatcher.drain()
    assert sorted(channel for channel, _ in sender.sent) == [CODE_CHANNEL, OTHER_CHANNEL]


async def test_filters_of_the_project_are_applied(client, dispatcher, sender, projects, deliveries, game):
    projects.set_route(GUILD, game.id, "code", CODE_CHANNEL, {"branches": ["main"]})
    payload = app_payload(gh.push_payload, ref="refs/heads/dev")
    status, body = await post_github(client, payload, delivery="d-dev")
    assert body["status"] == "recorded"
    await dispatcher.drain()
    assert sender.sent == [] and deliveries.get_status(game.id, "github:push:d-dev") == "filtered"


async def test_category_without_channel_is_ignored(client, dispatcher, sender, game):
    status, body = await post_github(client, app_payload(gh.issue_payload), event="issues")
    assert (status, body["status"]) == (200, "ignored")
    await dispatcher.drain()
    assert sender.sent == []


async def test_same_delivery_or_same_fact_is_notified_once(client, dispatcher, sender, game):
    payload = app_payload(gh.push_payload)
    assert (await post_github(client, payload, delivery="d-1"))[1]["status"] == "queued"
    assert (await post_github(client, payload, delivery="d-1"))[1]["status"] == "duplicate"
    assert (await post_github(client, payload, delivery="d-2"))[1]["status"] == "duplicate"
    await dispatcher.drain()
    assert len(sender.sent) == 1


async def test_ping_and_unhandled_events(client, sender, game):
    assert (await post_github(client, app_payload(gh.ping_payload), event="ping"))[1]["status"] == "pong"
    for event in ("installation", "installation_repositories", "star"):
        status, body = await post_github(client, app_payload(gh.push_payload), event=event)
        assert (status, body["status"]) == (200, "ignored")
    assert sender.sent == []


async def test_notifications_are_restored_after_a_restart(projects, deliveries, sender, channels, game):
    async def channel_guild(channel_id):
        return channels.get(channel_id)

    first = ProjectDispatcher(projects, deliveries, sender, channel_guild, retry_delays=())
    event = translate_github("push", "d-1", gh.push_payload())
    assert first.submit(event, [game]) == "queued"
    restarted = ProjectDispatcher(projects, deliveries, sender, channel_guild, retry_delays=())
    assert restarted.restore_pending() == 1
    await restarted.drain()
    assert [channel for channel, _ in sender.sent] == [CODE_CHANNEL]


# --- GitHub App : accès refusés ------------------------------------------------------------------


async def test_unlinked_or_foreign_repository_is_ignored(client, dispatcher, sender, deliveries, projects, game):
    projects.create(GUILD, "github", "equipe/autre", "Sans lien", created_by=1)
    for payload in (
        app_payload(gh.push_payload, installation=INSTALLATION + 1),  # installation de quelqu'un d'autre
        app_payload(gh.push_payload, repository={**gh.REPOSITORY, "full_name": "equipe/autre"}),  # projet sans lien
        app_payload(gh.push_payload, repository={**gh.REPOSITORY, "full_name": "inconnu/depot"}),
    ):
        status, body = await post_github(client, payload)
        assert (status, body["status"]) == (200, "ignored")
    await dispatcher.drain()
    assert sender.sent == [] and deliveries.last_received_at() is None


@pytest.mark.parametrize("signature", [False, "sha256=" + "0" * 64, "n'importe quoi"])
async def test_missing_or_invalid_signature_is_refused(client, dispatcher, sender, game, signature):
    status, body = await post_github(client, app_payload(gh.push_payload), signature=signature)
    assert (status, body["status"]) == (401, "unauthorized")
    await dispatcher.drain()
    assert sender.sent == []


async def test_signature_made_with_the_legacy_secret_is_refused(client, game):
    status, _ = await post_github(client, app_payload(gh.push_payload), secret="secret-github-de-test-0123456789")
    assert status == 401


async def test_payload_without_installation_is_refused(client, game):
    status, body = await post_github(client, app_payload(gh.push_payload, installation=None))
    assert (status, body["status"]) == (400, "invalid_payload")


async def test_malformed_requests_are_refused(client, game):
    assert (await post_github(client, b"{pas du json"))[0] == 400
    assert (await post_github(client, app_payload(gh.push_payload), content_type="text/plain"))[0] == 415
    big = b'{"a": "' + b"x" * MAX_BODY_BYTES + b'"}'
    assert (await post_github(client, big))[0] == 413


async def test_channel_of_another_server_is_never_used(client, dispatcher, sender, deliveries, db, game):
    # Salon d'un autre serveur écrit directement en base (contournement de /projet modifier).
    db.conn.execute("UPDATE project_routes SET channel_id = ? WHERE project_id = ? AND category = 'code'", (OTHER_CHANNEL, game.id))
    await post_github(client, app_payload(gh.push_payload), delivery="d-x")
    await dispatcher.drain()
    assert sender.sent == [] and deliveries.get_status(game.id, "github:push:d-x") == EventStatus.SKIPPED


async def test_deleted_channel_is_skipped(client, dispatcher, sender, deliveries, channels, game):
    del channels[CODE_CHANNEL]
    await post_github(client, app_payload(gh.push_payload), delivery="d-y")
    await dispatcher.drain()
    assert sender.sent == [] and deliveries.get_status(game.id, "github:push:d-y") == EventStatus.SKIPPED


async def test_github_path_is_closed_without_app_secret(projects, dispatcher, tools, sender):
    client = await _client(create_projects_app(github_secret="", projects=projects, dispatcher=dispatcher))
    try:
        assert (await client.post(GITHUB_PATH, data=b"{}", headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})).status == 404
        assert (await post_gitlab(client, gl.merge_request_payload()))[0] == 200  # GitLab reste disponible
    finally:
        await client.close()


async def test_only_two_paths_exist(client):
    assert (await client.get(GITHUB_PATH)).status == 405
    assert (await client.get(f"/gitlab/webhook/{HOOK}")).status == 405
    for path in ("/", "/admin", LEGACY_GITHUB_PATH, LEGACY_GITLAB_PATH, "/gitlab/connect"):
        assert (await client.post(path, data=b"{}")).status == 404


# --- GitLab : un webhook par projet --------------------------------------------------------------


async def test_gitlab_project_receives_its_event(client, dispatcher, sender, deliveries, tools):
    status, body = await post_gitlab(client, gl.merge_request_payload(), uuid_header="u-1")
    assert (status, body["status"]) == (200, "queued")
    await dispatcher.drain()
    [(channel, notification)] = sender.sent
    assert channel == CODE_CHANNEL and notification.kind == "merge_request"
    assert deliveries.get_status(tools.id, "gitlab:merge_request:u-1") == EventStatus.SENT


async def test_gitlab_project_registered_by_path(client, dispatcher, sender, projects):
    project = projects.create(GUILD, "gitlab", "epita/sae-game", "Par chemin", created_by=1)
    projects.set_route(GUILD, project.id, "ci", CI_CHANNEL)
    projects.link_gitlab(GUILD, project.id, "hook-par-chemin-0001", hash_secret(HOOK_SECRET))
    status, body = await post_gitlab(client, gl.pipeline_payload(), event="Pipeline Hook", hook="hook-par-chemin-0001")
    assert body["status"] == "queued"


async def test_gitlab_ci_status_filter(client, dispatcher, sender, projects, tools):
    projects.set_route(GUILD, tools.id, "ci", CI_CHANNEL, {"statuts": ["echoue"]})
    assert (await post_gitlab(client, gl.pipeline_payload(status="success"), event="Pipeline Hook"))[1]["status"] == "recorded"
    assert (await post_gitlab(client, gl.pipeline_payload(status="failed", id=32), event="Pipeline Hook"))[1]["status"] == "queued"
    await dispatcher.drain()
    assert [channel for channel, _ in sender.sent] == [CI_CHANNEL]


async def test_unknown_hook_and_wrong_secret_get_the_same_answer(client, dispatcher, sender, tools):
    answers = [
        await post_gitlab(client, gl.merge_request_payload(), token="mauvais-secret-0123456789"),
        await post_gitlab(client, gl.merge_request_payload(), token=None),
        await post_gitlab(client, gl.merge_request_payload(), hook="hook-inconnu-00000001"),
        await post_gitlab(client, gl.merge_request_payload(), hook="court"),
    ]
    assert all(answer == (401, {"status": "unauthorized", "detail": "secret invalide"}) for answer in answers)
    await dispatcher.drain()
    assert sender.sent == []


async def test_secret_of_another_projects_hook_is_refused(client, projects, tools):
    other = projects.create(OTHER_GUILD, "gitlab", "777", "Leur projet", created_by=9)
    projects.link_gitlab(OTHER_GUILD, other.id, "hook-autre-serveur-001", hash_secret("secret-de-l-autre-serveur-01"))
    status, _ = await post_gitlab(client, gl.merge_request_payload(), hook="hook-autre-serveur-001", token=HOOK_SECRET)
    assert status == 401


async def test_event_of_another_gitlab_project_is_refused(client, dispatcher, sender, tools):
    payload = gl.merge_request_payload()
    payload["project"] = {**payload["project"], "id": 9999, "path_with_namespace": "autre/projet"}
    status, body = await post_gitlab(client, payload)
    assert (status, body["status"]) == (403, "forbidden_project")
    await dispatcher.drain()
    assert sender.sent == []


async def test_unhandled_gitlab_event_is_ignored(client, tools):
    status, body = await post_gitlab(client, {"object_kind": "issue"}, event="Issue Hook")
    assert (status, body["status"]) == (200, "ignored")


async def test_secrets_never_appear_in_responses_or_logs(client, game, tools, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = [
        await post_gitlab(client, gl.merge_request_payload(), token="mauvais-secret-0123456789"),
        await post_gitlab(client, gl.merge_request_payload()),
        await post_github(client, app_payload(gh.push_payload)),
        await post_github(client, app_payload(gh.push_payload), signature="sha256=" + "0" * 64),
    ]
    text = json.dumps(bodies) + caplog.text
    for secret in (APP_SECRET, HOOK_SECRET, hash_secret(HOOK_SECRET), "mauvais-secret-0123456789"):
        assert secret not in text


# --- Visibilité dans /projet (règle des projets masqués) -----------------------------------------


async def test_connected_project_follows_the_masking_rule(make_bot):  # noqa: F811
    import discord

    from tests.fake_guild import FakeGuild
    from tests.test_projects import Member, embed_text, run

    bot = await make_bot()
    guild = FakeGuild()
    member = Member()
    public, private = guild.add_channel("activité-git"), guild.add_channel("equipe-privee")
    private.overwrites[member] = discord.PermissionOverwrite(view_channel=False)
    shown = bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Jeu connecté", created_by=1)
    bot.projects_repo.set_route(GUILD, shown.id, "code", public.id)
    bot.projects_repo.link_github(GUILD, shown.id, INSTALLATION)
    hidden = bot.projects_repo.create(GUILD, "github", "equipe/prototype", "Prototype connecté", created_by=1)
    bot.projects_repo.set_route(GUILD, hidden.id, "code", private.id)
    bot.projects_repo.link_github(GUILD, hidden.id, INSTALLATION)

    text = embed_text((await run(bot, "liste", guild, member))["embed"])
    assert "Jeu connecté" in text and "connecté : notifications actives" in text and "Prototype" not in text
    unknown = embed_text((await run(bot, "details", guild, member, projet="999"))["embed"])
    assert embed_text((await run(bot, "details", guild, member, projet=str(hidden.id)))["embed"]) == unknown
    admin = embed_text((await run(bot, "liste", guild, Member(8, admin=True)))["embed"])
    assert "Prototype connecté" in admin


# --- Configuration et compatibilité avec le .env -------------------------------------------------


def config(**env) -> Settings:
    return Settings.from_env({"DISCORD_GUILD_ID": str(GUILD), **env}, require_token=False)


def test_reception_is_disabled_by_default_and_env_webhooks_are_untouched():
    legacy = {"GITHUB_WEBHOOK_SECRET": "secret-github-de-test-0123456789", "GITLAB_WEBHOOK_SECRET": "secret-gitlab-de-test-0123456789"}
    before = config(**legacy)
    after = config(**legacy, PROJECTS_HTTP_ENABLED="true", GITHUB_APP_WEBHOOK_SECRET=APP_SECRET)
    assert not before.projects_http_enabled and before.projects_http_error is None
    assert after.projects_http_enabled and (after.projects_http_host, after.projects_http_port) == ("127.0.0.1", 8090)
    for name in ("github_webhook_secret", "github_repository", "github_webhook_port", "gitlab_webhook_secret", "webhook_port"):
        assert getattr(before, name) == getattr(after, name)
    assert APP_SECRET not in repr(after)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PROJECTS_HTTP_HOST": "0.0.0.0"}, "adresse locale"),
        ({"PROJECTS_HTTP_PORT": "8081", "GITHUB_WEBHOOK_SECRET": "secret-github-de-test-0123456789"}, "GITHUB_WEBHOOK_PORT"),
        ({"PROJECTS_HTTP_PORT": "8080", "GITLAB_WEBHOOK_SECRET": "secret-gitlab-de-test-0123456789"}, "WEBHOOK_PORT"),
        ({"PROJECTS_HTTP_PORT": "abc"}, "PROJECTS_HTTP_PORT"),
        ({"GITHUB_APP_WEBHOOK_SECRET": "court"}, "trop court"),
        ({"GITHUB_APP_WEBHOOK_SECRET": "secret-github-de-test-0123456789", "GITHUB_WEBHOOK_SECRET": "secret-github-de-test-0123456789"}, "différent"),
    ],
)
def test_invalid_reception_settings_disable_only_the_reception(env, message):
    settings = config(PROJECTS_HTTP_ENABLED="true", **env)
    assert not settings.projects_http_enabled and message in settings.projects_http_error
    assert settings.github_app_webhook_secret == ""


def test_invalid_flag_is_reported():
    settings = config(PROJECTS_HTTP_ENABLED="peut-être")
    assert not settings.projects_http_enabled and "PROJECTS_HTTP_ENABLED" in settings.projects_http_error


def test_app_secret_is_masked_in_logs():
    from src.config import secret_values

    assert APP_SECRET in secret_values({"GITHUB_APP_WEBHOOK_SECRET": APP_SECRET})


def test_health_line_never_counts_projects():
    assert "désactivée" in _projects_check(ProjectsReceptionSummary()).detail
    assert "invalide" in _projects_check(ProjectsReceptionSummary(error="configuration invalide : x")).detail
    ok = _projects_check(ProjectsReceptionSummary(enabled=True, listening=True, address="http://127.0.0.1:8090"))
    assert "127.0.0.1:8090" in ok.detail and "GitHub App fermée" in ok.detail and "projet" not in ok.detail


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_bot_starts_the_reception_only_when_enabled(make_bot):  # noqa: F811
    disabled = await make_bot()
    await disabled.start_project_reception()
    assert disabled.projects_server is None and disabled.projects_error is None

    port = _free_port()
    bot = await make_bot(PROJECTS_HTTP_ENABLED="true", PROJECTS_HTTP_PORT=str(port))
    await bot.start_project_reception()
    try:
        assert bot.projects_server.listening and bot.projects_server.local_url == f"http://127.0.0.1:{port}"
        assert bot.projects_server.host == "127.0.0.1"
    finally:
        await bot.projects_server.stop()


from tests.test_projects import make_bot  # noqa: E402, F401  (fixture)
