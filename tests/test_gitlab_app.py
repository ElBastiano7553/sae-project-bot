"""Multi-projets, étape B5 : connexion des projets GitLab par une application OAuth en lecture seule.

Les tests de bout en bout suivent le vrai parcours, avec de vraies requêtes HTTP sur des ports
locaux temporaires : lien d'autorisation donné par /projet configurer projet: → retour sur
/gitlab/callback (un faux GitLab local répond aux appels d'API) → page qui affiche une seule fois
l'adresse et le secret du webhook → événement GitLab envoyé avec ce secret, reçu par la réception
(B3) → notification dans le salon du projet. Aucun appel à Internet.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web

from src.config import SECRET_ENV_VARS, Settings
from src.database import Database
from src.database.connection import MIGRATIONS
from src.database.github_setup import GithubSetupRepository, utcnow
from src.database.gitlab_setup import GitlabSetupRepository
from src.discord_bot.project_views import GitlabConnectView
from src.gitlab import samples as gl
from src.gitlab.dispatcher import EventStatus
from src.providers.dispatcher import ProjectDispatcher
from src.providers.github_app import STATE_TTL, SetupStates
from src.providers.gitlab_oauth import CALLBACK_PATH, STATE_PURPOSE, STEP, build_gitlab_flow
from src.providers.server import create_projects_app
from src.services.health import ProjectsReceptionSummary, _projects_check
from tests.fake_guild import FakeGuild
from tests.test_github_app import CLIENT_SECRET as GITHUB_CLIENT_SECRET
from tests.test_github_app import APP_ENV as GITHUB_APP_ENV
from tests.test_github_app import Clock, FakeSender, Listening, click, last_text, quiet_client
from tests.test_projects import GUILD, OTHER_GUILD, Interaction, Member, embed_text, make_bot  # noqa: F401

GITLAB_CLIENT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
GITLAB_CLIENT_SECRET = "gloas-secret-oauth-de-test-0123456789"
PUBLIC_URL = "https://samae.exemple.fr"
GITLAB_ENV = {
    "PROJECTS_HTTP_ENABLED": "true",
    "PROJECTS_PUBLIC_URL": PUBLIC_URL,
    "GITLAB_APP_CLIENT_ID": GITLAB_CLIENT_ID,
    "GITLAB_APP_CLIENT_SECRET": GITLAB_CLIENT_SECRET,
}
PROJECT_PATH, PROJECT_NUMBER = "epita/sae-game", 4242  # comme src/gitlab/samples.py
MAINTAINER, DEVELOPER, OWNER = 40, 30, 50


def settings(**env) -> Settings:
    return Settings.from_env({"DISCORD_GUILD_ID": str(GUILD), **env}, require_token=False)


# --- Configuration ------------------------------------------------------------------------------


def test_connection_is_disabled_by_default_and_legacy_settings_are_unchanged():
    base = settings(GITLAB_WEBHOOK_SECRET="secret-gitlab-historique-0123")
    assert not base.gitlab_app_enabled and base.gitlab_app_error is None
    with_app = settings(GITLAB_WEBHOOK_SECRET="secret-gitlab-historique-0123", **GITLAB_ENV)
    assert with_app.gitlab_app_enabled and with_app.gitlab_app_url == "https://gitlab.com"
    assert with_app.projects_public_url == PUBLIC_URL
    for name in ("gitlab_webhook_secret", "gitlab_project_id", "webhook_port", "gitlab_base_url", "github_app_enabled", "projects_http_port"):
        assert getattr(base, name) == getattr(with_app, name), name


def test_gitlab_only_does_not_report_a_github_app_error():
    config = settings(**GITLAB_ENV)
    assert config.gitlab_app_enabled and config.github_app_error is None and not config.github_app_enabled


def test_both_connections_can_be_enabled_together():
    config = settings(**{**GITHUB_APP_ENV, **GITLAB_ENV})
    assert config.github_app_enabled and config.gitlab_app_enabled and config.projects_public_url == PUBLIC_URL


def test_client_secret_is_masked_everywhere():
    config = settings(**GITLAB_ENV)
    assert GITLAB_CLIENT_SECRET not in repr(config)
    assert "GITLAB_APP_CLIENT_SECRET" in SECRET_ENV_VARS


def test_self_hosted_instance_is_accepted():
    config = settings(**GITLAB_ENV, GITLAB_APP_URL="https://gitlab.cri.epita.fr/")
    assert config.gitlab_app_enabled and config.gitlab_app_url == "https://gitlab.cri.epita.fr"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"GITLAB_APP_CLIENT_SECRET": ""}, "GITLAB_APP_CLIENT_SECRET manquant"),
        ({"GITLAB_APP_CLIENT_ID": ""}, "GITLAB_APP_CLIENT_ID manquant"),
        ({"GITLAB_APP_CLIENT_ID": "court"}, "Application ID"),
        ({"GITLAB_APP_CLIENT_SECRET": "court"}, "trop court"),
        ({"GITLAB_APP_URL": "http://gitlab.exemple.fr"}, "GITLAB_APP_URL"),
        ({"GITLAB_APP_URL": "https://jean:mdp@gitlab.exemple.fr"}, "GITLAB_APP_URL"),
        ({"PROJECTS_PUBLIC_URL": ""}, "PROJECTS_PUBLIC_URL manquante"),
        ({"PROJECTS_PUBLIC_URL": "https://samae.exemple.fr/chemin"}, "sans chemin"),
        ({"PROJECTS_HTTP_ENABLED": "false"}, "réception des projets doit être active"),
        ({"GITLAB_WEBHOOK_SECRET": GITLAB_CLIENT_SECRET}, "différent des autres secrets"),
    ],
)
def test_invalid_connection_settings_only_disable_the_connection(changes, message):
    config = settings(**{**GITLAB_ENV, **changes})
    assert not config.gitlab_app_enabled and message in config.gitlab_app_error
    assert GITLAB_CLIENT_SECRET not in config.gitlab_app_error
    assert config.guild_id == GUILD


def test_health_line_shows_both_connections():
    base = {"enabled": True, "listening": True, "address": "http://127.0.0.1:8090", "github_app": True}
    detail = _projects_check(ProjectsReceptionSummary(**base, github_connect=True, gitlab_connect=True)).detail
    assert "connexion GitHub active" in detail and "connexion GitLab active" in detail
    refused = _projects_check(ProjectsReceptionSummary(**base, gitlab_connect_error="GITLAB_APP_CLIENT_ID manquant(s)"))
    assert refused.status.name == "WARNING" and "connexion GitLab refusée" in refused.detail


# --- Migration v12 et jetons ------------------------------------------------------------------------


def test_v11_database_is_upgraded_to_v12_with_a_backup(tmp_path):
    path = tmp_path / "v11.sqlite3"
    conn = sqlite3.connect(path)
    for version, script in enumerate(MIGRATIONS[:11], start=1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("INSERT INTO projects (guild_id, provider, external_id, instance_url, name, created_by) VALUES (42, 'gitlab', 'a/b', 'https://gitlab.com', 'Jeu', 1)")
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db.schema_version == len(MIGRATIONS) >= 12
        assert db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert GitlabSetupRepository(db).pending_states() == 0
    finally:
        db.close()
    assert len(list((tmp_path / "backups").glob(f"*-avantv{len(MIGRATIONS)}.sqlite3"))) == 1


@pytest.fixture
def gitlab_states(db):
    return GitlabSetupRepository(db)


def _project(db, guild=GUILD, external_id=PROJECT_PATH, instance="https://gitlab.com"):
    from src.database.projects import ProjectRepository

    return ProjectRepository(db).create(guild, "gitlab", external_id, "Jeu", created_by=1, instance_url=instance)


def test_state_is_single_use_bound_to_its_project_and_stored_only_as_a_hash(db, gitlab_states):
    project = _project(db)
    states = SetupStates(gitlab_states, GITLAB_CLIENT_SECRET, purpose=STATE_PURPOSE)
    token = states.issue(STEP, GUILD, 1, project_id=project.id)
    stored = [row[0] for row in db.conn.execute("SELECT token_hash FROM gitlab_setup_states")]
    assert token not in stored
    claim = states.consume(STEP, token)
    assert (claim.guild_id, claim.user_id, claim.project_id) == (GUILD, 1, project.id)
    assert states.consume(STEP, token) is None


def test_state_expires_and_github_tokens_are_not_gitlab_tokens(db, gitlab_states):
    project = _project(db)
    clock = Clock()
    states = SetupStates(gitlab_states, GITLAB_CLIENT_SECRET, clock=clock, purpose=STATE_PURPOSE)
    expired = states.issue(STEP, GUILD, 1, project_id=project.id)
    clock.now += STATE_TTL + timedelta(seconds=1)
    assert states.consume(STEP, expired) is None
    # Même secret, autre parcours : la signature d'un jeton GitHub ne vaut rien pour GitLab.
    github_token = SetupStates(GithubSetupRepository(db), GITLAB_CLIENT_SECRET, clock=clock).issue("install", GUILD, 1)
    assert states.consume(STEP, github_token) is None


def test_deleting_the_project_deletes_its_pending_links(db, gitlab_states):
    from src.database.projects import ProjectRepository

    project = _project(db)
    SetupStates(gitlab_states, GITLAB_CLIENT_SECRET, purpose=STATE_PURPOSE).issue(STEP, GUILD, 1, project_id=project.id)
    ProjectRepository(db).delete(GUILD, project.id)
    assert gitlab_states.pending_states() == 0


# --- Faux GitLab local ------------------------------------------------------------------------------


class FakeGitLab:
    """Imite les quelques adresses de GitLab utilisées par Samae, et note ce qu'elles reçoivent."""

    def __init__(self) -> None:
        self.codes: dict[str, str] = {}  # code OAuth → nom d'utilisateur
        self.users = {"romain": 7, "dev": 8, "herite": 9, "autre": 10}
        # (utilisateur) → rôle direct sur le projet, rôle hérité (members/all)
        self.direct = {"romain": MAINTAINER, "dev": DEVELOPER, "herite": None}
        self.inherited = {"herite": OWNER}
        self.project = {"id": PROJECT_NUMBER, "path_with_namespace": "Epita/SAE-Game"}
        self.aliases: set[str] = set()  # anciens chemins encore redirigés par GitLab (projet renommé)
        self.tokens: dict[str, str] = {}
        self.revoked: list[str] = []
        self.token_status = 200
        self.api_status = 200
        self.requests: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/oauth/token", self.token)
        app.router.add_post("/oauth/revoke", self.revoke)
        app.router.add_get("/api/v4/user", self.user)
        app.router.add_get(r"/api/v4/projects/{id:\d+}/members/all/{uid:\d+}", self.member)
        app.router.add_get("/api/v4/projects/{ref:.+}", self.project_info)
        return app

    async def token(self, request):
        form = await request.post()
        self.requests.append(f"token redirect={form.get('redirect_uri')} grant={form.get('grant_type')}")
        if self.token_status != 200:
            return web.json_response({"error": "server_error"}, status=self.token_status)
        if form.get("client_id") != GITLAB_CLIENT_ID or form.get("client_secret") != GITLAB_CLIENT_SECRET:
            return web.json_response({"error": "invalid_client"}, status=401)
        user = self.codes.pop(form.get("code"), None)
        if user is None:
            return web.json_response({"error": "invalid_grant"}, status=400)
        token = f"glpat-{uuid.uuid4().hex}"
        self.tokens[token] = user
        return web.json_response({"access_token": token, "token_type": "Bearer", "scope": "read_user read_api"})

    async def revoke(self, request):
        form = await request.post()
        assert form.get("client_id") == GITLAB_CLIENT_ID and form.get("client_secret") == GITLAB_CLIENT_SECRET
        self.revoked.append(form.get("token"))
        return web.json_response({})

    def _user(self, request) -> str | None:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return None if token in self.revoked else self.tokens.get(token)

    async def user(self, request):
        name = self._user(request)
        if name is None:
            return web.json_response({"message": "401 Unauthorized"}, status=401)
        return web.json_response({"id": self.users[name], "username": name})

    async def project_info(self, request):
        name = self._user(request)
        self.requests.append(f"project {request.match_info['ref']}")
        if name is None:
            return web.json_response({"message": "401 Unauthorized"}, status=401)
        if self.api_status != 200:
            return web.json_response({"message": "error"}, status=self.api_status)
        wanted = request.match_info["ref"].casefold()
        known = {str(self.project["id"]), self.project["path_with_namespace"].casefold(), *self.aliases}
        if wanted not in known or name == "autre":
            return web.json_response({"message": "404 Project Not Found"}, status=404)
        direct = self.direct.get(name)
        permissions = {"project_access": {"access_level": direct} if direct else None, "group_access": None}
        return web.json_response({**self.project, "permissions": permissions})

    async def member(self, request):
        user_id = int(request.match_info["uid"])
        name = next(n for n, i in self.users.items() if i == user_id)
        level = self.inherited.get(name) or self.direct.get(name)
        if level is None:
            return web.json_response({"message": "404 Not found"}, status=404)
        return web.json_response({"id": user_id, "username": name, "access_level": level})


@pytest.fixture
async def world(make_bot):  # noqa: F811
    """Bot avec la connexion GitLab activée, faux GitLab, réception des projets sur un port local."""
    gitlab = FakeGitLab()
    gitlab_client = await quiet_client(gitlab.app())
    base = str(gitlab_client.make_url("")).rstrip("/")

    bot = await make_bot(**GITLAB_ENV)
    bot.gitlab_setup = build_gitlab_flow(bot.settings, bot.gitlab_setup_repo, bot.projects_repo, base_url=base, on_linked=bot.record_gitlab_link)
    bot.projects_server = Listening()
    guild = FakeGuild()
    channel = guild.add_channel("code-jeu")
    channel.mention = f"<#{channel.id}>"
    channel.messages = []

    async def send(*, embed, _channel=channel):
        _channel.messages.append(embed)
        return SimpleNamespace(channel=_channel)

    channel.send = send
    sender = FakeSender()

    async def channel_guild(channel_id):
        return GUILD if channel_id == channel.id else None

    dispatcher = ProjectDispatcher(bot.projects_repo, bot.project_deliveries_repo, sender, channel_guild, retry_delays=())
    app = create_projects_app(github_secret="", projects=bot.projects_repo, dispatcher=dispatcher, gitlab_setup=bot.gitlab_setup)
    samae = await quiet_client(app)
    project = bot.projects_repo.create(GUILD, "gitlab", PROJECT_PATH, "Jeu", created_by=1, instance_url="https://gitlab.com")
    bot.projects_repo.set_route(GUILD, project.id, "code", channel.id)

    w = SimpleNamespace(bot=bot, gitlab=gitlab, samae=samae, guild=guild, channel=channel, sender=sender, dispatcher=dispatcher, project=project)
    yield w
    await samae.close()
    await gitlab_client.close()


async def configure(w, ref=None, user=None) -> dict:
    cog = w.bot.get_cog("ProjectsCog")
    interaction = Interaction(w.bot, w.guild, user or Member(1, admin=True))
    await cog.configure.callback(cog, interaction, projet=ref or str(w.project.id))
    return interaction.response.sent[0]


def authorize_url(sent: dict) -> str:
    return next(item.url for item in sent["view"].children if getattr(item, "url", None))


async def browser(w, url: str, *, user="romain", deny=False, code=None, state=None) -> tuple[int, str]:
    """Ce que fait le navigateur : autorisation sur GitLab, puis retour sur Samae."""
    query = parse_qs(urlsplit(url).query)
    params = {"state": state or query["state"][0]}
    if deny:
        params["error"] = "access_denied"
    else:
        code = code or f"code-{uuid.uuid4().hex[:12]}"
        w.gitlab.codes[code] = user
        params["code"] = code
    response = await w.samae.get(CALLBACK_PATH, params=params)
    return response.status, await response.text()


def webhook_from(page: str) -> tuple[str, str]:
    url = re.search(r"URL : <code>([^<]+)</code>", page).group(1)
    secret = re.search(r"Jeton secret : <code>([^<]+)</code>", page).group(1)
    return urlsplit(url).path, secret


async def push(w, path: str, secret: str, *, project_id=PROJECT_NUMBER, project_path="Epita/SAE-Game", uuid_header=None):
    payload = gl.push_payload()
    payload["project_id"] = project_id
    payload["project"] = {**payload["project"], "id": project_id, "path_with_namespace": project_path}
    headers = {"Content-Type": "application/json", "X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": secret}
    if uuid_header:
        headers["X-Gitlab-Event-UUID"] = uuid_header
    response = await w.samae.post(path, data=json.dumps(payload).encode("utf-8"), headers=headers)
    return response.status, (await response.json())["status"]


# --- Bout en bout -------------------------------------------------------------------------------------


async def test_end_to_end_authorize_show_secret_once_receive_and_notify(world, caplog):
    w = world
    caplog.set_level(logging.DEBUG)
    sent = await configure(w)
    assert sent["ephemeral"] and isinstance(sent["view"], GitlabConnectView)
    text = embed_text(sent["embed"])
    assert "15 minutes" in text and "une seule fois" in text and "Maintainer" in text
    url = authorize_url(sent)
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/oauth/authorize"
    assert query["client_id"] == [GITLAB_CLIENT_ID] and query["redirect_uri"] == [f"{PUBLIC_URL}{CALLBACK_PATH}"]
    assert query["scope"] == ["read_user read_api"] and query["response_type"] == ["code"]

    status, page = await browser(w, url)
    assert status == 200 and "Projet relié" in page and "ne sera plus jamais affiché" in page
    path, secret = webhook_from(page)
    assert path.startswith("/gitlab/webhook/") and len(secret) >= 40
    assert len(w.gitlab.revoked) == 1  # accès temporaire révoqué aussitôt
    link = w.bot.projects_repo.link(GUILD, w.project.id)
    assert link.hook_id == path.rsplit("/", 1)[1]

    # Le secret n'est gardé nulle part en clair : ni en base, ni dans les journaux.
    dump = "\n".join(w.bot.database.conn.iterdump())
    assert secret not in dump and secret not in caplog.text and GITLAB_CLIENT_SECRET not in caplog.text
    assert not any(token in caplog.text or token in dump for token in w.gitlab.tokens)
    assert [a.action for a in w.bot.admin_log.recent()][:1] == ["projet configurer"]

    # Réception (B3) : l'événement du projet relié, avec ce secret, arrive dans le salon « Code ».
    assert await push(w, path, secret, uuid_header="livraison-1") == (200, "queued")
    await w.dispatcher.drain()
    [(channel_id, notification)] = w.sender.sent
    assert channel_id == w.channel.id and notification.kind == "push"
    assert await push(w, path, secret, uuid_header="livraison-1") == (200, "duplicate")

    # Même lien rechargé : le secret ne réapparaît pas.
    status, again = await browser(w, url, state=query["state"][0])
    assert status == 400 and "Lien expiré" in again and secret not in again


async def test_pages_are_not_cached_and_send_no_referrer(world):
    w = world
    response = await w.samae.get(CALLBACK_PATH, params={"state": "x", "code": "y"})
    assert response.headers["Cache-Control"] == "no-store" and response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


async def test_wrong_secret_or_another_project_is_refused(world):
    w = world
    _, page = await browser(w, authorize_url(await configure(w)))
    path, secret = webhook_from(page)
    assert await push(w, path, "mauvais-secret-0123456789") == (401, "unauthorized")
    assert await push(w, path, secret, project_id=1, project_path="autre/projet") == (403, "forbidden_project")
    assert w.sender.sent == []


async def test_inherited_owner_role_is_enough(world):
    w = world
    status, page = await browser(w, authorize_url(await configure(w)), user="herite")
    assert status == 200 and "Projet relié" in page


async def test_developer_role_is_refused_and_nothing_is_linked(world):
    w = world
    status, page = await browser(w, authorize_url(await configure(w)), user="dev")
    assert status == 403 and "Developer" in page and "Maintainer" in page
    assert w.bot.projects_repo.link(GUILD, w.project.id) is None
    assert len(w.gitlab.revoked) == 1


async def test_invisible_project_is_refused(world):
    w = world
    status, page = await browser(w, authorize_url(await configure(w)), user="autre")
    assert status == 403 and "ne voit pas ce projet" in page
    assert w.bot.projects_repo.link(GUILD, w.project.id) is None


async def test_renamed_project_is_refused_rather_than_guessed(world):
    w = world
    # GitLab redirige l'ancien chemin vers le projet renommé : Samae refuse plutôt que de deviner.
    w.gitlab.project["path_with_namespace"] = "epita/autre-nom"
    w.gitlab.aliases.add(PROJECT_PATH)
    status, _ = await browser(w, authorize_url(await configure(w)))
    assert status == 403 and w.bot.projects_repo.link(GUILD, w.project.id) is None


async def test_denied_authorization_links_nothing(world):
    w = world
    status, page = await browser(w, authorize_url(await configure(w)), deny=True)
    assert status == 400 and "Autorisation annulée" in page
    assert w.bot.projects_repo.link(GUILD, w.project.id) is None


async def test_reused_code_is_refused(world):
    w = world
    assert (await browser(w, authorize_url(await configure(w)), code="code-unique"))[0] == 200
    url = authorize_url(await configure(w))
    response = await w.samae.get(CALLBACK_PATH, params={"state": parse_qs(urlsplit(url).query)["state"][0], "code": "code-unique"})
    assert response.status == 400 and "Autorisation refusée" in await response.text()


@pytest.mark.parametrize(("where", "status"), [("token", 500), ("api", 500), ("api", 429)])
async def test_gitlab_unavailable_shows_a_retry_page(world, where, status):
    w = world
    if where == "token":
        w.gitlab.token_status = status
    else:
        w.gitlab.api_status = status
    code, page = await browser(w, authorize_url(await configure(w)))
    assert code == 502 and "GitLab injoignable" in page
    assert w.bot.projects_repo.link(GUILD, w.project.id) is None


async def test_project_deleted_before_the_return_is_refused(world):
    w = world
    url = authorize_url(await configure(w))
    w.bot.projects_repo.delete(GUILD, w.project.id)
    status, page = await browser(w, url)
    assert status == 400 and ("Lien expiré" in page or "introuvable" in page)


async def test_reconnecting_replaces_the_secret(world):
    w = world
    _, first = await browser(w, authorize_url(await configure(w)))
    old_path, old_secret = webhook_from(first)
    sent = await configure(w)
    assert "nouveau secret" in embed_text(sent["embed"])
    _, second = await browser(w, authorize_url(sent))
    new_path, new_secret = webhook_from(second)
    assert "ancien secret ne fonctionne plus" in second and new_secret != old_secret
    assert await push(w, old_path, old_secret) == (401, "unauthorized")
    assert await push(w, new_path, new_secret) == (200, "queued")


async def test_legacy_env_webhook_on_the_same_project_is_refused(make_bot):  # noqa: F811
    bot = await make_bot(**GITLAB_ENV, GITLAB_WEBHOOK_SECRET="secret-gitlab-historique-0123", GITLAB_PROJECT_ID=str(PROJECT_NUMBER))
    bot.projects_server = Listening()
    project = bot.projects_repo.create(GUILD, "gitlab", str(PROJECT_NUMBER), "Jeu", created_by=1, instance_url="https://gitlab.com")
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, FakeGuild(), Member(1, admin=True))
    await cog.configure.callback(cog, interaction, projet=str(project.id))
    assert "env-gitlab" in embed_text(interaction.response.sent[0]["embed"])
    assert bot.gitlab_setup_repo.pending_states() == 0


async def test_legacy_duplicate_found_only_at_the_return_is_refused(make_bot):  # noqa: F811
    """Projet enregistré par son chemin : son numéro n'est connu qu'au retour de GitLab."""
    bot = await make_bot(**GITLAB_ENV, GITLAB_WEBHOOK_SECRET="secret-gitlab-historique-0123", GITLAB_PROJECT_ID=str(PROJECT_NUMBER))
    gitlab = FakeGitLab()
    client = await quiet_client(gitlab.app())
    try:
        bot.gitlab_setup = build_gitlab_flow(bot.settings, bot.gitlab_setup_repo, bot.projects_repo, base_url=str(client.make_url("")).rstrip("/"))
        dispatcher = ProjectDispatcher(bot.projects_repo, bot.project_deliveries_repo, FakeSender(), lambda _: None, retry_delays=())
        samae = await quiet_client(create_projects_app(github_secret="", projects=bot.projects_repo, dispatcher=dispatcher, gitlab_setup=bot.gitlab_setup))
        project = bot.projects_repo.create(GUILD, "gitlab", PROJECT_PATH, "Jeu", created_by=1, instance_url="https://gitlab.com")
        url = bot.gitlab_setup.authorize_url(GUILD, 1, project.id)
        w = SimpleNamespace(gitlab=gitlab, samae=samae)
        status, page = await browser(w, url)
        assert status == 409 and "env-gitlab" in page
        assert bot.projects_repo.link(GUILD, project.id) is None
        await samae.close()
    finally:
        await client.close()


# --- Discord ----------------------------------------------------------------------------------------


async def test_disabled_connection_says_so_and_creates_nothing(make_bot):  # noqa: F811
    bot = await make_bot()
    project = bot.projects_repo.create(GUILD, "gitlab", PROJECT_PATH, "Jeu", created_by=1)
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, FakeGuild(), Member(1, admin=True))
    await cog.configure.callback(cog, interaction, projet=str(project.id))
    assert "pas encore activée" in embed_text(interaction.response.sent[0]["embed"])
    assert bot.gitlab_setup_repo.pending_states() == 0


async def test_github_project_reference_points_to_the_depot_option(world):
    w = world
    github = w.bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Jeu GitHub", created_by=1)
    text = embed_text((await configure(w, ref=str(github.id)))["embed"])
    assert "depot:equipe/jeu" in text


@pytest.mark.parametrize("ref", ["999", "abc", "env-gitlab"])
async def test_unknown_reference_is_refused(world, ref):
    text = embed_text((await configure(world, ref=ref))["embed"])
    assert "introuvable" in text


async def test_another_guilds_project_is_refused(world):
    w = world
    foreign = w.bot.projects_repo.create(OTHER_GUILD, "gitlab", "autre/projet", "Autre", created_by=1)
    assert "introuvable" in embed_text((await configure(w, ref=str(foreign.id)))["embed"])
    assert w.bot.gitlab_setup_repo.pending_states() == 0


async def test_project_on_another_instance_is_refused(world):
    w = world
    other = w.bot.projects_repo.create(GUILD, "gitlab", "equipe/outil", "Outil", created_by=1, instance_url="https://gitlab.cri.epita.fr")
    text = embed_text((await configure(w, ref=str(other.id)))["embed"])
    assert "gitlab.cri.epita.fr" in text and "https://gitlab.com" in text


async def test_stopped_reception_is_refused(world):
    w = world
    w.bot.projects_server = None
    assert "arrêtée" in embed_text((await configure(w))["embed"])


async def test_test_button_appears_once_linked_with_channels(world):
    w = world
    sent = await configure(w)
    assert not any(getattr(item, "label", "") == "Envoyer un message de test" for item in sent["view"].children)
    await browser(w, authorize_url(sent))
    sent = await configure(w)
    view = sent["view"]
    assert any(getattr(item, "label", "") == "Envoyer un message de test" for item in view.children)
    done = await click(w, view, "test")
    assert "Message de test envoyé et confirmé" in last_text(done)
    assert len(w.channel.messages) == 1


async def test_autocomplete_offers_only_this_guilds_gitlab_projects_to_admins(world):
    w = world
    w.bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Jeu GitHub", created_by=1)
    w.bot.projects_repo.create(OTHER_GUILD, "gitlab", "autre/projet", "Autre", created_by=1)
    cog = w.bot.get_cog("ProjectsCog")
    admin = Interaction(w.bot, w.guild, Member(1, admin=True))
    choices = await cog._gitlab_autocomplete(admin, "")
    assert [c.value for c in choices] == [str(w.project.id)]
    member = Interaction(w.bot, w.guild, Member(2))
    assert await cog._gitlab_autocomplete(member, "") == []


async def test_delivery_status_of_the_test_push(world):
    w = world
    _, page = await browser(w, authorize_url(await configure(w)))
    path, secret = webhook_from(page)
    await push(w, path, secret, uuid_header="essai")
    await w.dispatcher.drain()
    assert w.bot.project_deliveries_repo.get_status(w.project.id, "gitlab:push:essai") == EventStatus.SENT
