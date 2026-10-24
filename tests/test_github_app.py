"""Multi-projets, étape B4 : connexion des projets GitHub par la GitHub App.

Les tests de bout en bout suivent le vrai parcours, avec de vraies requêtes HTTP sur des ports
locaux temporaires : lien d'installation donné par /projet configurer → page /github/setup →
autorisation OAuth → page /github/callback (un faux GitHub local répond aux appels d'API) →
choix du dépôt et confirmation dans Discord → événement signé reçu par la réception (B3) →
notification dans le salon du projet. Aucun appel à Internet.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.config import PROJECT_ROOT, SECRET_ENV_VARS, Settings
from src.database import Database
from src.database.connection import MIGRATIONS
from src.database.github_setup import GithubSetupRepository, utcnow
from src.discord_bot.project_views import GithubConnectView, RepositorySelectView, TestMessageView
from src.github import samples as gh
from src.github.security import compute_signature
from src.providers.check import FILTER_TEST_BRANCH, run_checks
from src.providers.dispatcher import ProjectDispatcher
from src.providers.github_app import CALLBACK_PATH, SETUP_PATH, STATE_TTL, SetupStates, build_flow
from src.providers.server import GITHUB_PATH, create_projects_app
from src.providers.translate import GITHUB_EVENTS
from src.services.health import ProjectsReceptionSummary, _projects_check
from tests.fake_guild import FakeGuild
from tests.test_projects import GITHUB_SECRET, GUILD, OTHER_GUILD, Interaction, Member, embed_text, make_bot  # noqa: F401
from tests.test_projects_add import ConfirmClick

APP_SECRET = "secret-github-app-de-test-0123456789"
CLIENT_ID = "Iv23liSamaeTest01"
CLIENT_SECRET = "client-secret-de-test-abcdef0123456789"
PUBLIC_URL = "https://samae.exemple.fr"
APP_ENV = {
    "PROJECTS_HTTP_ENABLED": "true",
    "GITHUB_APP_WEBHOOK_SECRET": APP_SECRET,
    "PROJECTS_PUBLIC_URL": PUBLIC_URL,
    "GITHUB_APP_SLUG": "samae-test",
    "GITHUB_APP_CLIENT_ID": CLIENT_ID,
    "GITHUB_APP_CLIENT_SECRET": CLIENT_SECRET,
}
INSTALLATION, OTHER_INSTALLATION = 5501, 7702
REPO, SECOND_REPO, FOREIGN_REPO = "Equipe/Depot-Test", "Equipe/Outils", "Autre/Prive"


def settings(**env) -> Settings:
    return Settings.from_env({"DISCORD_GUILD_ID": str(GUILD), **env}, require_token=False)


# --- Configuration ------------------------------------------------------------------------------


def test_connection_is_disabled_by_default_and_legacy_settings_are_unchanged():
    base = settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    assert not base.github_app_enabled and base.github_app_error is None
    with_app = settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, **APP_ENV)
    assert with_app.github_app_enabled and with_app.projects_public_url == PUBLIC_URL
    for name in ("github_webhook_secret", "github_repository", "github_webhook_port", "projects_http_port", "gitlab_webhook_secret"):
        assert getattr(base, name) == getattr(with_app, name), name


def test_client_secret_is_masked_everywhere():
    config = settings(**APP_ENV)
    assert CLIENT_SECRET not in repr(config) and APP_SECRET not in repr(config)
    assert "GITHUB_APP_CLIENT_SECRET" in SECRET_ENV_VARS


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"GITHUB_APP_SLUG": ""}, "GITHUB_APP_SLUG manquant"),
        ({"PROJECTS_PUBLIC_URL": "http://samae.exemple.fr"}, "https://"),
        ({"PROJECTS_PUBLIC_URL": "https://samae.exemple.fr/chemin"}, "sans chemin"),
        ({"PROJECTS_PUBLIC_URL": "https://jean:mdp@samae.exemple.fr"}, "sans chemin ni identifiants"),
        ({"GITHUB_APP_SLUG": "Samae Test"}, "nom court"),
        ({"GITHUB_APP_CLIENT_ID": "x"}, "Client ID"),
        ({"GITHUB_APP_CLIENT_SECRET": "court"}, "trop court"),
        ({"GITHUB_APP_CLIENT_SECRET": APP_SECRET}, "différent des secrets"),
        ({"PROJECTS_HTTP_ENABLED": "false"}, "réception des projets doit être active"),
        ({"GITHUB_APP_WEBHOOK_SECRET": ""}, "réception des projets doit être active"),
    ],
)
def test_invalid_connection_settings_only_disable_the_connection(changes, message):
    config = settings(**{**APP_ENV, **changes})
    assert not config.github_app_enabled and message in config.github_app_error
    assert CLIENT_SECRET not in config.github_app_error
    assert config.guild_id == GUILD  # le reste du bot démarre


def test_health_line_shows_the_connection_state_without_counting_projects():
    base = {"enabled": True, "listening": True, "address": "http://127.0.0.1:8090", "github_app": True}
    assert "connexion GitHub active" in _projects_check(ProjectsReceptionSummary(**base, github_connect=True)).detail
    assert "non configurée" in _projects_check(ProjectsReceptionSummary(**base)).detail
    refused = _projects_check(ProjectsReceptionSummary(**base, github_connect_error="GITHUB_APP_SLUG manquant"))
    assert refused.status.name == "WARNING" and "refusée" in refused.detail


# --- Migration v11 ------------------------------------------------------------------------------


def test_v10_database_is_upgraded_to_v11_with_a_backup(tmp_path):
    path = tmp_path / "v10.sqlite3"
    conn = sqlite3.connect(path)
    for version, script in enumerate(MIGRATIONS[:10], start=1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("INSERT INTO projects (guild_id, provider, external_id, name, created_by) VALUES (42, 'github', 'a/b', 'Jeu', 1)")
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert db.schema_version == len(MIGRATIONS) >= 11
        assert db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert GithubSetupRepository(db).installations(42) == []
    finally:
        db.close()
    assert len(list((tmp_path / "backups").glob(f"*-avantv{len(MIGRATIONS)}.sqlite3"))) == 1


def test_state_table_refuses_inconsistent_rows(db):
    for values in (("x" * 64, "install", 1), ("x" * 64, "oauth", None), ("court", "install", None), ("y" * 64, "autre", None)):
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO github_setup_states (token_hash, step, guild_id, user_id, installation_id, expires_at) VALUES (?, ?, 1, 1, ?, '2099-01-01')",
                (values[0], values[1], values[2]),
            )


# --- Jetons « state » -----------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self):
        return self.now


@pytest.fixture
def setup_repo(db):
    return GithubSetupRepository(db)


def test_state_is_single_use_bound_to_its_step_and_stored_only_as_a_hash(db, setup_repo):
    states = SetupStates(setup_repo, CLIENT_SECRET)
    token = states.issue("install", GUILD, 1)
    stored = [row[0] for row in db.conn.execute("SELECT token_hash FROM github_setup_states")]
    assert token not in stored and token.split(".")[0] not in json.dumps(stored)
    assert states.consume("oauth", token) is None  # autre étape
    claim = states.consume("install", token)
    assert (claim.guild_id, claim.user_id, claim.installation_id) == (GUILD, 1, None)
    assert states.consume("install", token) is None  # déjà utilisé


def test_state_expires_and_refuses_forged_or_foreign_tokens(setup_repo):
    clock = Clock()
    states = SetupStates(setup_repo, CLIENT_SECRET, clock=clock)
    expired = states.issue("install", GUILD, 1)
    clock.now += STATE_TTL + timedelta(seconds=1)
    assert states.consume("install", expired) is None
    token = states.issue("install", GUILD, 1)
    nonce, signature = token.split(".")
    forged = f"{nonce}.{'A' * len(signature)}"
    other_key = SetupStates(setup_repo, "un-autre-secret-oauth-0123456789", clock=clock).issue("install", GUILD, 1)
    for bad in (forged, other_key, "", None, "pas-un-jeton", token + "x"):
        assert states.consume("install", bad) is None
    assert states.consume("install", token) is not None


# --- Faux GitHub local ------------------------------------------------------------------------------


class FakeGitHub:
    """Imite les quelques adresses de GitHub utilisées par Samae, et note ce qu'elles reçoivent."""

    def __init__(self) -> None:
        # code OAuth → compte GitHub ; compte → installations auxquelles il a accès
        self.codes: dict[str, str] = {}
        self.access: dict[str, set[int]] = {}
        self.installations = {
            INSTALLATION: ("equipe", [REPO, SECOND_REPO]),
            OTHER_INSTALLATION: ("autre", [FOREIGN_REPO]),
        }
        self.tokens: dict[str, str] = {}
        self.revoked: list[str] = []
        self.requests: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/login/oauth/access_token", self.token)
        app.router.add_get("/user/installations", self.user_installations)
        app.router.add_get("/user/installations/{id}/repositories", self.repositories)
        app.router.add_delete("/applications/{client_id}/token", self.revoke)
        return app

    async def token(self, request):
        form = await request.post()
        self.requests.append(f"token redirect={form.get('redirect_uri')}")
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return web.json_response({"error": "incorrect_client_credentials"})
        user = self.codes.pop(form.get("code"), None)  # un code ne sert qu'une fois
        if user is None:
            return web.json_response({"error": "bad_verification_code"})
        token = f"ghu_{uuid.uuid4().hex}"
        self.tokens[token] = user
        return web.json_response({"access_token": token, "token_type": "bearer"})

    def _user(self, request) -> str | None:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return None if token in self.revoked else self.tokens.get(token)

    async def user_installations(self, request):
        user = self._user(request)
        if user is None:
            return web.json_response({"message": "Bad credentials"}, status=401)
        mine = [{"id": i, "account": {"login": self.installations[i][0]}} for i in sorted(self.access.get(user, set()))]
        return web.json_response({"total_count": len(mine), "installations": mine})

    async def repositories(self, request):
        user = self._user(request)
        installation = int(request.match_info["id"])
        if user is None or installation not in self.access.get(user, set()):
            return web.json_response({"message": "Not Found"}, status=404)
        repos = [{"full_name": name} for name in self.installations[installation][1]]
        return web.json_response({"total_count": len(repos), "repositories": repos})

    async def revoke(self, request):
        scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
        assert scheme == "Basic" and base64.b64decode(encoded).decode() == f"{CLIENT_ID}:{CLIENT_SECRET}"
        self.revoked.append((await request.json())["access_token"])
        return web.Response(status=204)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, object]] = []

    async def send_notification(self, channel_id, notification):
        self.sent.append((channel_id, notification))


class Listening:
    """Réception des projets « en écoute » (le vrai serveur est remplacé par un serveur de test)."""

    listening = True

    async def stop(self) -> None:
        pass


async def quiet_client(app: web.Application) -> TestClient:
    """Comme ProjectsHttpServer (``access_log=None``) : les adresses, qui portent des jetons, ne sont pas journalisées."""
    server = TestServer(app)
    await server.start_server(access_log=None)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.fixture
async def world(make_bot):  # noqa: F811
    """Bot avec la connexion GitHub activée, faux GitHub, réception des projets sur un port local."""
    github = FakeGitHub()
    github_client = await quiet_client(github.app())
    base = str(github_client.make_url("")).rstrip("/")

    bot = await make_bot(**APP_ENV)
    bot.github_setup = build_flow(bot.settings, bot.github_setup_repo, github_url=base, api_url=base)
    bot.projects_server = Listening()
    guild = FakeGuild()
    channels = {}
    for name in ("code-depot", "ci-depot"):
        channel = guild.add_channel(name)
        channel.mention = f"<#{channel.id}>"
        channel.messages = []

        async def send(*, embed, _channel=channel):
            _channel.messages.append(embed)

        channel.send = send
        channels[name] = channel
    elsewhere = FakeGuild()
    elsewhere.id = OTHER_GUILD
    foreign_channel = elsewhere.add_channel("autre-serveur")

    sender = FakeSender()
    where = {c.id: GUILD for c in channels.values()} | {foreign_channel.id: OTHER_GUILD}

    async def channel_guild(channel_id):
        return where.get(channel_id)

    dispatcher = ProjectDispatcher(bot.projects_repo, bot.project_deliveries_repo, sender, channel_guild, retry_delays=())
    app = create_projects_app(github_secret=APP_SECRET, projects=bot.projects_repo, dispatcher=dispatcher, github_setup=bot.github_setup)
    samae = await quiet_client(app)

    class World:
        pass

    w = World()
    w.bot, w.github, w.samae, w.guild, w.channels, w.sender, w.dispatcher = bot, github, samae, guild, channels, sender, dispatcher
    w.foreign_channel = foreign_channel
    yield w
    await samae.close()
    await github_client.close()


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def github_round_trip(w, install_url: str, *, installation=INSTALLATION, user="romain", grant=True, code=None):
    """Ce que fait le navigateur : retour d'installation, autorisation sur GitHub, retour d'autorisation."""
    response = await w.samae.get(
        SETUP_PATH, params={"installation_id": str(installation), "setup_action": "install", "state": state_of(install_url)}, allow_redirects=False
    )
    if response.status != 302:
        return response.status, await response.text()
    location = response.headers["Location"]
    query = parse_qs(urlsplit(location).query)
    assert urlsplit(location).path == "/login/oauth/authorize"
    assert query["client_id"] == [CLIENT_ID] and query["redirect_uri"] == [f"{PUBLIC_URL}{CALLBACK_PATH}"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
    if grant:
        w.github.access.setdefault(user, set()).add(installation)
    code = code or f"code-{uuid.uuid4().hex[:12]}"
    w.github.codes[code] = user
    callback = await w.samae.get(CALLBACK_PATH, params={"code": code, "state": query["state"][0]})
    return callback.status, await callback.text()


async def configure(w, user=None, **options) -> tuple[Interaction, dict]:
    cog = w.bot.get_cog("ProjectsCog")
    interaction = Interaction(w.bot, w.guild, user or Member(1, admin=True))
    w.guild.name = "SAE · GAME DEV"
    await cog.configure.callback(cog, interaction, **options)
    return interaction, interaction.response.sent[0]


async def click(w, view, button: str, user=None) -> ConfirmClick:
    """Clic sur un bouton d'une vue, par ``user`` (l'administrateur n° 1 par défaut), sur le serveur du test."""
    interaction = ConfirmClick(w.bot, user or Member(1, admin=True))
    interaction.guild = w.guild
    if await view.interaction_check(interaction):
        await getattr(view, button).callback(interaction)
    return interaction


async def connect_through_discord(w, repository: str = REPO, user=None) -> dict:
    """/projet configurer → « Choisir le dépôt » → menu → « Confirmer ». Renvoie le dernier affichage."""
    _, sent = await configure(w, user=user)
    chooser = await click(w, sent["view"], "choose", user)
    menu = chooser.response.sent[-1]
    assert isinstance(menu["view"], RepositorySelectView), menu
    select_view = menu["view"]
    select_view.select._values = [repository]
    pick = ConfirmClick(w.bot, user or Member(1, admin=True))
    pick.guild = w.guild
    await select_view._selected(pick)
    preview = pick.response.sent[-1]
    confirmation = await click(w, preview["view"], "confirm", user)
    return confirmation.edits[-1]


async def push(w, repository=REPO, installation=INSTALLATION, *, delivery=None, secret=APP_SECRET, signed=True, branch="main", sha=None):
    payload = gh.push_payload()
    payload["repository"] = {**payload["repository"], "full_name": repository}
    payload["installation"] = {"id": installation}
    payload["ref"] = f"refs/heads/{branch}"
    if sha:
        payload["after"] = sha
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "push", "X-GitHub-Delivery": delivery or str(uuid.uuid4())}
    if signed:
        headers["X-Hub-Signature-256"] = compute_signature(secret, body)
    response = await w.samae.post(GITHUB_PATH, data=body, headers=headers)
    return response.status, (await response.json())["status"]


# --- Bout en bout : parcours complet ------------------------------------------------------------------


async def test_end_to_end_install_verify_connect_receive_and_test_message(world):
    w = world
    interaction, sent = await configure(w)
    assert sent["ephemeral"] and isinstance(sent["view"], GithubConnectView)
    text = embed_text(sent["embed"])
    assert "15 minutes" in text and "Aucun token" in text
    assert "/apps/samae-test/installations/new?state=" in state_url(sent)

    status, page = await github_round_trip(w, state_url(sent))
    assert status == 200 and "Installation vérifiée" in page and "2 dépôt(s)" in page
    assert len(w.github.revoked) == 1  # l'accès temporaire est révoqué aussitôt
    [installation] = w.bot.github_setup_repo.installations(GUILD)
    assert (installation.installation_id, installation.account, installation.authorized_by) == (INSTALLATION, "equipe", 1)
    assert set(installation.repositories) == {REPO, SECOND_REPO}

    # Le projet a déjà ses salons (enregistré avant avec /projet ajouter, puis /projet modifier).
    project = w.bot.projects_repo.create(GUILD, "github", REPO, "Dépôt de test", created_by=1)
    w.bot.projects_repo.set_route(GUILD, project.id, "code", w.channels["code-depot"].id, {"branches": ["main"]})
    w.bot.projects_repo.set_route(GUILD, project.id, "ci", w.channels["ci-depot"].id)
    assert await push(w) == (200, "ignored")  # pas encore relié : rien n'est accepté

    final = await connect_through_discord(w)
    assert "Projet connecté" in embed_text(final["embed"])
    assert w.bot.projects_repo.link(GUILD, project.id).installation_id == INSTALLATION
    assert [a.action for a in w.bot.admin_log.recent()][:1] == ["projet configurer"]

    # Réception (B3) : l'événement signé du dépôt connecté arrive dans le salon du projet.
    assert await push(w, delivery="livraison-1") == (200, "queued")
    await w.dispatcher.drain()
    [(channel_id, _notification)] = w.sender.sent
    assert channel_id == w.channels["code-depot"].id

    # Message de test : dans chacun des deux salons du projet, une seule fois.
    tester = final["view"]
    assert isinstance(tester, TestMessageView)
    done = await click(w, tester, "test")
    assert "Message de test envoyé" in done.response.sent[-1]["content"]
    assert len(w.channels["code-depot"].messages) == len(w.channels["ci-depot"].messages) == 1
    again = await click(w, tester, "test")
    assert "déjà été envoyé" in again.response.sent[-1]["content"]
    assert len(w.channels["code-depot"].messages) == 1


async def test_end_to_end_signatures_duplicates_and_filters(world):
    w = world
    _, sent = await configure(w)
    await github_round_trip(w, state_url(sent))
    project = w.bot.projects_repo.create(GUILD, "github", REPO, "Dépôt de test", created_by=1)
    w.bot.projects_repo.set_route(GUILD, project.id, "code", w.channels["code-depot"].id, {"branches": ["main", "release/*"]})
    await connect_through_discord(w)

    # Signatures
    assert await push(w, signed=False) == (401, "unauthorized")
    assert await push(w, secret="faux-secret-0123456789abcdef") == (401, "unauthorized")
    assert await push(w, secret=CLIENT_SECRET) == (401, "unauthorized")  # le secret OAuth ne signe rien
    # Doublons : même livraison, puis même fait sous une autre livraison
    assert await push(w, delivery="d-1", sha="a" * 40) == (200, "queued")
    assert await push(w, delivery="d-1", sha="a" * 40) == (200, "duplicate")
    assert await push(w, delivery="d-2", sha="a" * 40) == (200, "duplicate")
    # Filtres de branches
    assert await push(w, branch="release/1.0", sha="b" * 40) == (200, "queued")
    assert await push(w, branch="experimental", sha="c" * 40, delivery="d-filtre") == (200, "recorded")
    assert w.bot.project_deliveries_repo.get_status(project.id, "github:push:d-filtre") == "filtered"
    await w.dispatcher.drain()
    assert len(w.sender.sent) == 2  # un seul message par fait accepté, rien pour la branche écartée


def state_url(sent) -> str:
    return next(item.url for item in sent["view"].children if getattr(item, "url", None))


# --- Isolation entre projets et serveurs ----------------------------------------------------------------


async def test_installation_verified_for_another_server_is_never_offered_here(world):
    w = world
    # Un administrateur d'un autre serveur relie SON installation (lien émis pour son serveur).
    foreign_url = w.bot.github_setup.install_url(OTHER_GUILD, 99)
    status, _ = await github_round_trip(w, foreign_url, installation=OTHER_INSTALLATION, user="autre")
    assert status == 200
    assert w.bot.github_setup_repo.installations(GUILD) == []
    # Ici : aucun dépôt proposé, et le dépôt de l'autre serveur ne peut pas être forcé par l'option.
    _, sent = await configure(w)
    chooser = await click(w, sent["view"], "choose")
    assert "Aucun dépôt" in embed_text(chooser.response.sent[-1]["embed"])
    _, refused = await configure(w, depot=FOREIGN_REPO)
    assert "aucune installation" in embed_text(refused["embed"])
    assert w.bot.projects_repo.list_for_guild(GUILD) == []


async def test_same_repository_in_two_servers_stays_isolated(world):
    w = world
    _, sent = await configure(w)
    await github_round_trip(w, state_url(sent))
    await connect_through_discord(w)
    [mine] = w.bot.projects_repo.list_for_guild(GUILD)
    w.bot.projects_repo.set_route(GUILD, mine.id, "code", w.channels["code-depot"].id)
    # L'autre serveur vérifie la même installation de son côté et relie le même dépôt à son salon.
    status, _ = await github_round_trip(w, w.bot.github_setup.install_url(OTHER_GUILD, 99), user="autre")
    assert status == 200
    theirs = w.bot.projects_repo.create(OTHER_GUILD, "github", REPO, "Leur copie", created_by=99)
    w.bot.projects_repo.set_route(OTHER_GUILD, theirs.id, "code", w.foreign_channel.id)
    w.bot.projects_repo.link_github(OTHER_GUILD, theirs.id, INSTALLATION)

    assert await push(w, delivery="partage") == (200, "queued")
    await w.dispatcher.drain()
    assert sorted(channel for channel, _ in w.sender.sent) == sorted([w.channels["code-depot"].id, w.foreign_channel.id])
    # Deux projets, chacun ses salons : un salon de l'autre serveur n'est jamais utilisé pour le mien.
    w.sender.sent.clear()
    w.bot.projects_repo.set_route(GUILD, mine.id, "code", w.foreign_channel.id)  # base modifiée à la main
    assert await push(w, delivery="detourne", sha="d" * 40) == (200, "queued")
    await w.dispatcher.drain()
    # Seul le projet de l'autre serveur publie dans son salon ; le mien est bloqué à la revérification.
    assert [channel for channel, _ in w.sender.sent] == [w.foreign_channel.id]


async def test_other_project_of_the_same_installation_receives_nothing_until_connected(world):
    w = world
    _, sent = await configure(w)
    await github_round_trip(w, state_url(sent))
    await connect_through_discord(w, REPO)
    [project] = w.bot.projects_repo.list_for_guild(GUILD)
    w.bot.projects_repo.set_route(GUILD, project.id, "code", w.channels["code-depot"].id)
    assert await push(w, SECOND_REPO) == (200, "ignored")  # même installation, dépôt non connecté
    assert await push(w, REPO, OTHER_INSTALLATION) == (200, "ignored")  # bon dépôt, autre installation


# --- Pages de retour : refus -------------------------------------------------------------------------------


async def test_state_links_cannot_be_reused_expired_or_forged(world):
    w = world
    url = w.bot.github_setup.install_url(GUILD, 1)
    assert (await github_round_trip(w, url))[0] == 200
    status, page = await github_round_trip(w, url)  # même lien une seconde fois
    assert status == 400 and "déjà utilisé" in page
    for state in ("", "faux", state_of(url)[:-1] + "A"):
        response = await w.samae.get(SETUP_PATH, params={"installation_id": "1", "state": state})
        assert response.status == 400
    response = await w.samae.get(CALLBACK_PATH, params={"code": "abc", "state": state_of(w.bot.github_setup.install_url(GUILD, 1))})
    assert response.status == 400  # un jeton d'installation ne vaut pas autorisation


async def test_person_without_access_to_the_installation_links_nothing(world):
    w = world
    status, page = await github_round_trip(w, w.bot.github_setup.install_url(GUILD, 1), installation=OTHER_INSTALLATION, grant=False)
    assert status == 403 and "Accès refusé" in page
    assert w.bot.github_setup_repo.installations(GUILD) == []
    assert len(w.github.revoked) == 1  # révoqué même en cas de refus


async def test_swapped_installation_number_is_refused(world):
    w = world
    # La personne a accès à INSTALLATION, mais l'adresse de retour annonce l'installation d'un autre.
    w.github.access["romain"] = {INSTALLATION}
    status, page = await github_round_trip(w, w.bot.github_setup.install_url(GUILD, 1), installation=OTHER_INSTALLATION, grant=False)
    assert status == 403
    assert w.bot.github_setup_repo.installations(GUILD) == []


async def test_refused_or_cancelled_authorization_and_github_outage(world):
    w = world
    url = w.bot.github_setup.install_url(GUILD, 1)
    response = await w.samae.get(SETUP_PATH, params={"installation_id": str(INSTALLATION), "state": state_of(url)}, allow_redirects=False)
    oauth_state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
    cancelled = await w.samae.get(CALLBACK_PATH, params={"error": "access_denied", "state": oauth_state})
    assert cancelled.status == 400 and "annulée" in await cancelled.text()

    response = await w.samae.get(
        SETUP_PATH, params={"installation_id": str(INSTALLATION), "state": state_of(w.bot.github_setup.install_url(GUILD, 1))}, allow_redirects=False
    )
    oauth_state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
    bad_code = await w.samae.get(CALLBACK_PATH, params={"code": "inconnu", "state": oauth_state})
    assert bad_code.status == 400 and "refusé" in await bad_code.text()

    w.bot.github_setup.client.github_url = "http://127.0.0.1:9"  # GitHub injoignable
    response = await w.samae.get(
        SETUP_PATH, params={"installation_id": str(INSTALLATION), "state": state_of(w.bot.github_setup.install_url(GUILD, 1))}, allow_redirects=False
    )
    oauth_state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
    outage = await w.samae.get(CALLBACK_PATH, params={"code": "abc", "state": oauth_state})
    assert outage.status == 502


async def test_installation_request_needing_an_owner_and_bad_installation_number(world):
    w = world
    request = await w.samae.get(SETUP_PATH, params={"setup_action": "request"})
    assert request.status == 200 and "Demande envoyée" in await request.text()
    bad = await w.samae.get(SETUP_PATH, params={"installation_id": "12abc", "state": state_of(w.bot.github_setup.install_url(GUILD, 1))})
    assert bad.status == 400


async def test_pages_are_never_cached_never_leak_referrer_and_escape_received_text(world):
    w = world
    w.github.installations[INSTALLATION] = ("<script>alert(1)</script>", [REPO])
    status, page = await github_round_trip(w, w.bot.github_setup.install_url(GUILD, 1))
    assert status == 200 and "<script>" not in page
    response = await w.samae.get(SETUP_PATH, params={"setup_action": "request"})
    assert response.headers["Cache-Control"] == "no-store" and response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert (await w.samae.post(SETUP_PATH)).status == 405
    assert (await w.samae.get("/github/autre")).status == 404


async def test_setup_pages_do_not_exist_without_the_connection(db):
    projects_app = create_projects_app(
        github_secret=APP_SECRET, projects=None, dispatcher=None  # type: ignore[arg-type]
    )
    client = TestClient(TestServer(projects_app))
    await client.start_server()
    try:
        assert (await client.get(SETUP_PATH)).status == 404
        assert (await client.get(CALLBACK_PATH)).status == 404
    finally:
        await client.close()


async def test_no_token_code_or_secret_in_pages_logs_or_database(world, caplog):
    w = world
    caplog.set_level(logging.DEBUG)
    status, page = await github_round_trip(w, w.bot.github_setup.install_url(GUILD, 1), code="code-secret-unique")
    assert status == 200
    [token] = w.github.revoked
    dump = "\n".join(str(row) for table in ("github_setup_states", "github_installations") for row in w.bot.github_setup_repo._conn.execute(f"SELECT * FROM {table}"))
    for secret in (token, "code-secret-unique", CLIENT_SECRET, APP_SECRET):
        assert secret not in page and secret not in caplog.text and secret not in dump


# --- Discord : droits et cas particuliers ---------------------------------------------------------------------


async def test_members_are_refused_and_autocomplete_is_empty_for_them(world):
    w = world
    cog = w.bot.get_cog("ProjectsCog")
    [check] = cog.configure.checks
    assert await check(Interaction(w.bot, w.guild, Member(2))) is False
    await github_round_trip(w, w.bot.github_setup.install_url(GUILD, 1))
    member = Interaction(w.bot, w.guild, Member(2))
    assert await cog._repository_autocomplete(member, "") == []
    admin = Interaction(w.bot, w.guild, Member(1, admin=True))
    assert [c.value for c in await cog._repository_autocomplete(admin, "")] == [REPO, SECOND_REPO]


async def test_buttons_recheck_that_the_person_is_still_an_admin(world):
    w = world
    await github_round_trip(w, w.bot.github_setup.install_url(GUILD, 1))
    _, sent = await configure(w)
    # La personne a perdu ses droits entre la commande et le clic.
    demoted = Member(1, admin=False)
    chooser = await click(w, sent["view"], "choose", demoted)
    assert "administrateurs" in embed_text(chooser.response.sent[-1]["embed"])
    # Quelqu'un d'autre ne peut pas utiliser les boutons.
    intruder = await click(w, sent["view"], "choose", Member(3, admin=True))
    assert "Seule la personne" in intruder.response.sent[-1]["content"]


async def test_connection_unavailable_without_configuration_or_reception(make_bot):  # noqa: F811
    bot = await make_bot()
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, FakeGuild(), Member(1, admin=True))
    await cog.configure.callback(cog, interaction)
    text = embed_text(interaction.response.sent[0]["embed"])
    assert "pas encore activée" in text and "/projet ajouter" in text

    bot = await make_bot(**APP_ENV)
    interaction = Interaction(bot, FakeGuild(), Member(1, admin=True))
    await cog_of(bot).configure.callback(cog_of(bot), interaction)
    assert "réception" in embed_text(interaction.response.sent[0]["embed"])


def cog_of(bot):
    return bot.get_cog("ProjectsCog")


async def test_legacy_env_repository_and_already_connected_repository_are_refused(make_bot):  # noqa: F811
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, **APP_ENV)
    bot.projects_server = Listening()
    bot.github_setup_repo.save_installation(GUILD, INSTALLATION, "ElBastiano7553", ["ElBastiano7553/game-project", REPO], authorized_by=1)
    cog = cog_of(bot)
    interaction = Interaction(bot, FakeGuild(), Member(1, admin=True))
    await cog.configure.callback(cog, interaction, depot="ElBastiano7553/game-project")
    assert "env-github" in embed_text(interaction.response.sent[0]["embed"])

    project = bot.projects_repo.create(GUILD, "github", REPO, "Déjà là", created_by=1)
    bot.projects_repo.link_github(GUILD, project.id, INSTALLATION)
    interaction = Interaction(bot, FakeGuild(), Member(1, admin=True))
    await cog.configure.callback(cog, interaction, depot=REPO.lower())
    shown = interaction.response.sent[0]
    assert "Projet connecté : Déjà là" in embed_text(shown["embed"]) and "view" not in shown  # pas de salon : pas de test
    guild = FakeGuild()
    channel = guild.add_channel("code")
    bot.projects_repo.set_route(GUILD, project.id, "code", channel.id)
    interaction = Interaction(bot, guild, Member(1, admin=True))
    await cog.configure.callback(cog, interaction, depot=REPO)
    assert isinstance(interaction.response.sent[0]["view"], TestMessageView)  # relancer suffit pour tester à nouveau
    assert [c.value for c in await cog._repository_autocomplete(Interaction(bot, FakeGuild(), Member(1, admin=True)), "")] == [
        "ElBastiano7553/game-project"
    ]


async def test_direct_option_creates_and_links_a_new_project_with_github_spelling(make_bot):  # noqa: F811
    bot = await make_bot(**APP_ENV)
    bot.projects_server = Listening()
    bot.github_setup_repo.save_installation(GUILD, INSTALLATION, "equipe", [REPO], authorized_by=1)
    cog = cog_of(bot)
    guild = FakeGuild()
    guild.name = "SAE"
    interaction = Interaction(bot, guild, Member(1, admin=True))
    await cog.configure.callback(cog, interaction, depot="equipe/depot-test")
    preview = interaction.response.sent[0]
    assert "nouveau projet « Depot-Test »" in embed_text(preview["embed"])
    confirmation = ConfirmClick(bot, Member(1, admin=True))
    confirmation.guild = guild
    await preview["view"].confirm.callback(confirmation)
    [project] = bot.projects_repo.list_for_guild(GUILD)
    assert (project.external_id, project.name) == ("equipe/depot-test", "Depot-Test")
    assert bot.projects_repo.link(GUILD, project.id).installation_id == INSTALLATION
    final = confirmation.edits[-1]
    assert "/projet modifier" in embed_text(final["embed"]) and final["view"] is None  # pas de salon : pas de bouton de test
    assert bot.projects_repo.routes(GUILD, project.id) == ()


async def test_install_link_is_personal_and_registered_for_this_server(world):
    w = world
    _, sent = await configure(w, Member(5, admin=True))
    url = state_url(sent)
    claim = w.bot.github_setup.states.consume("install", state_of(url))
    assert (claim.guild_id, claim.user_id) == (GUILD, 5)


async def test_discord_never_shows_a_secret(world):
    w = world
    _, sent = await configure(w)
    shown = embed_text(sent["embed"]) + " ".join(getattr(i, "url", "") or "" for i in sent["view"].children)
    for secret in (CLIENT_SECRET, APP_SECRET):
        assert secret not in shown


async def test_test_message_refuses_a_channel_samae_cannot_post_in(world):
    w = world
    project = w.bot.projects_repo.create(GUILD, "github", REPO, "Dépôt", created_by=1)
    w.bot.projects_repo.set_route(GUILD, project.id, "code", w.channels["code-depot"].id)
    w.guild.bot_permissions.discard("send_messages")
    view = TestMessageView(1, lambda inter: cog_of(w.bot)._send_test(inter, project.id))
    done = await click(w, view, "test")
    assert "Impossible de publier" in done.response.sent[-1]["content"]
    assert w.channels["code-depot"].messages == []


def test_discord_select_lists_at_most_25_repositories():
    async def noop(*_):
        return None

    view = RepositorySelectView(1, [f"equipe/depot-{i:02d}" for i in range(40)], noop)
    assert len(view.select.options) == 25


# --- Outil de contrôle sur le serveur (python -m src.providers.check) -------------------------------------


async def test_check_tool_passes_against_a_connected_project_and_publishes_one_message(world):
    w = world
    project = w.bot.projects_repo.create(GUILD, "github", REPO, "Dépôt de test", created_by=1)
    w.bot.projects_repo.set_route(GUILD, project.id, "code", w.channels["code-depot"].id, {"branches": ["main"]})
    w.bot.projects_repo.link_github(GUILD, project.id, INSTALLATION)
    url = str(w.samae.make_url(GITHUB_PATH))
    steps = await run_checks(url, APP_SECRET, REPO, INSTALLATION, legacy_secret=GITHUB_SECRET, filtered=True)
    assert [s.name for s in steps if not s.ok] == []
    assert len(steps) == 9 and any(FILTER_TEST_BRANCH in s.name for s in steps)
    await w.dispatcher.drain()
    assert [channel for channel, _ in w.sender.sent] == [w.channels["code-depot"].id]


async def test_check_tool_reports_failures_when_the_project_is_not_connected(world):
    w = world
    steps = await run_checks(str(w.samae.make_url(GITHUB_PATH)), APP_SECRET, REPO, INSTALLATION, filtered=False)
    failed = {s.name for s in steps if not s.ok}
    assert "événement valide → notification publiée" in failed  # dépôt non relié : ignoré, pas publié


# --- Fichiers fournis : GitHub App de test et exemple Nginx ----------------------------------------------


def test_test_app_file_asks_only_for_read_permissions_and_handled_events():
    manifest = json.loads((PROJECT_ROOT / "deploy" / "github-app" / "samae-test-app.json").read_text(encoding="utf-8"))
    assert set(manifest["default_permissions"]) == {"metadata", "contents", "pull_requests", "issues", "actions"}
    assert set(manifest["default_permissions"].values()) == {"read"}  # aucune écriture
    assert set(manifest["default_events"]) <= set(GITHUB_EVENTS)
    assert manifest["public"] is False and manifest["request_oauth_on_install"] is False and manifest["setup_on_update"] is True
    assert manifest["setup_url"].endswith(SETUP_PATH) and manifest["callback_urls"] == [f"{PUBLIC_URL}{CALLBACK_PATH}"]
    assert manifest["hook_attributes"]["url"].endswith(GITHUB_PATH)


def test_nginx_example_opens_only_the_needed_paths_and_never_logs_their_parameters():
    text = (PROJECT_ROOT / "deploy" / "nginx" / "samae.conf.example").read_text(encoding="utf-8")
    assert "location ~ ^/github/(setup|callback)$" in text and "limit_except GET" in text
    assert "access_log /var/log/nginx/samae.access.log samae_sans_parametres;" in text
    assert "$request_method $uri" in text and '"$request"' not in text
    assert "127.0.0.1:8090" in text and "return 404;" in text
