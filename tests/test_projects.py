"""Multi-projets, étape B1 : migration v9, dépôt des projets, projets historiques (.env), /projet liste et details."""

from __future__ import annotations

import sqlite3

import discord
import pytest

from src.config import Settings
from src.database import Database
from src.database.connection import MIGRATIONS
from src.database.projects import ProjectRepository
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.services.projects import (
    LEGACY_GITHUB,
    LEGACY_GITLAB,
    ProjectError,
    Route,
    legacy_projects,
    visible_routes,
)
from tests.fake_guild import FakeGuild

GUILD = 42
OTHER_GUILD = 77
GITHUB_SECRET = "github-secret-de-test-123456"
GITLAB_SECRET = "gitlab-secret-de-test-123456"


# --- Migration -------------------------------------------------------------------------------


def test_v8_database_is_upgraded_to_v9_without_losing_data(tmp_path):
    path = tmp_path / "v8.sqlite3"
    conn = sqlite3.connect(path)
    for version, script in enumerate(MIGRATIONS[:8], start=1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("INSERT INTO channel_bindings (key, channel_id) VALUES ('gitlab_activite', 5)")
    conn.execute("INSERT INTO github_deliveries (delivery_key, event_type, status) VALUES ('push:abc', 'push', 'sent')")
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db.schema_version == len(MIGRATIONS) == 9
        assert db.conn.execute("SELECT channel_id FROM channel_bindings WHERE key = 'gitlab_activite'").fetchone()[0] == 5
        assert db.conn.execute("SELECT status FROM github_deliveries").fetchone()[0] == "sent"
        # Les projets du .env ne sont pas copiés en base : ils restent lus du .env.
        assert db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM project_routes").fetchone()[0] == 0
    finally:
        db.close()
    backups = list((tmp_path / "backups").glob("*-avantv9.sqlite3"))
    assert len(backups) == 1  # sauvegarde automatique avant la migration


def test_schema_refuses_unknown_provider_category_and_status(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO projects (guild_id, provider, external_id, name, created_by) VALUES (1, 'svn', 'x', 'x', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO projects (guild_id, provider, external_id, name, status, created_by) VALUES (1, 'github', 'a/b', 'x', 'perdu', 1)"
        )
    project = ProjectRepository(db).create(1, "github", "a/b", "Jeu", created_by=1)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO project_routes (project_id, category, channel_id) VALUES (?, 'musique', 1)", (project.id,))


# --- Dépôt : validation et isolation entre serveurs --------------------------------------------


def test_create_validates_and_normalizes(db):
    repo = ProjectRepository(db)
    github = repo.create(GUILD, "github", " ElBastiano7553/game-project ", "  Jeu   du  projet ", created_by=7)
    # Identifiants en minuscules : GitHub et GitLab ignorent la casse (pas de doublon « Equipe/Jeu » / « equipe/jeu »).
    assert (github.provider, github.external_id, github.instance_url, github.name) == ("github", "elbastiano7553/game-project", "", "Jeu du projet")
    with pytest.raises(ProjectError, match="déjà suivi"):
        repo.create(GUILD, "github", "elbastiano7553/GAME-project", "Doublon", created_by=7)
    gitlab = repo.create(GUILD, "gitlab", "0042", "Outils", created_by=7, instance_url="https://gitlab.exemple.fr/")
    assert (gitlab.external_id, gitlab.instance_url, gitlab.status) == ("42", "https://gitlab.exemple.fr", "active")
    assert repo.create(GUILD, "gitlab", "43", "Défaut", created_by=7).instance_url == "https://gitlab.com"


@pytest.mark.parametrize(
    ("provider", "external_id", "name", "instance"),
    [
        ("github", "pas-de-barre", "x", ""),
        ("github", "a/b/c", "x", ""),
        ("gitlab", "abc", "x", ""),
        ("gitlab", "0", "x", ""),
        ("gitlab", "12", "x", "http://gitlab.exemple.fr"),
        ("gitlab", "12", "x", "https://jean:motdepasse@gitlab.exemple.fr"),
        ("github", "a/b", "   ", ""),
        ("github", "a/b", "x" * 81, ""),
        ("bitbucket", "a/b", "x", ""),
    ],
)
def test_create_refuses_invalid_data(db, provider, external_id, name, instance):
    with pytest.raises(ProjectError):
        ProjectRepository(db).create(GUILD, provider, external_id, name, created_by=7, instance_url=instance)


def test_same_repository_once_per_guild_but_allowed_on_another_guild(db):
    repo = ProjectRepository(db)
    repo.create(GUILD, "github", "a/b", "Jeu", created_by=7)
    with pytest.raises(ProjectError, match="déjà suivi"):
        repo.create(GUILD, "github", "a/b", "Autre nom", created_by=8)
    other = repo.create(OTHER_GUILD, "github", "a/b", "Jeu", created_by=9)
    assert other.guild_id == OTHER_GUILD


def test_a_guild_never_reads_or_changes_another_guilds_project(db):
    repo = ProjectRepository(db)
    mine = repo.create(GUILD, "github", "a/b", "Jeu", created_by=7)
    theirs = repo.create(OTHER_GUILD, "gitlab", "12", "Leur projet", created_by=9)
    repo.set_route(OTHER_GUILD, theirs.id, "code", 555)

    assert [p.id for p in repo.list_for_guild(GUILD)] == [mine.id]
    assert repo.get(GUILD, theirs.id) is None
    assert repo.routes(GUILD, theirs.id) == ()
    with pytest.raises(ProjectError):
        repo.set_route(GUILD, theirs.id, "code", 1)
    assert repo.remove_route(GUILD, theirs.id, "code") is False
    assert repo.delete(GUILD, theirs.id) is False
    # Rien n'a bougé chez l'autre serveur.
    assert repo.get(OTHER_GUILD, theirs.id) is not None
    assert repo.routes(OTHER_GUILD, theirs.id) == (Route("code", 555, {}),)


def test_routes_are_ordered_updated_and_deleted_with_the_project(db):
    repo = ProjectRepository(db)
    project = repo.create(GUILD, "github", "a/b", "Jeu", created_by=7)
    repo.set_route(GUILD, project.id, "ci", 3)
    repo.set_route(GUILD, project.id, "code", 1, {"branches": ["main"]})
    repo.set_route(GUILD, project.id, "ci", 4)  # remplace le salon précédent
    assert repo.routes(GUILD, project.id) == (Route("code", 1, {"branches": ["main"]}), Route("ci", 4, {}))
    with pytest.raises(ProjectError):
        repo.set_route(GUILD, project.id, "musique", 1)
    assert repo.remove_route(GUILD, project.id, "ci") is True
    assert repo.delete(GUILD, project.id) is True
    assert db.conn.execute("SELECT COUNT(*) FROM project_routes").fetchone()[0] == 0


# --- Projets historiques (.env) -----------------------------------------------------------------


def settings(**env) -> Settings:
    return Settings.from_env({"DISCORD_GUILD_ID": str(GUILD), **env}, require_token=False)


def legacy(config: Settings, guild_id: int = GUILD, routes=None, bindings=None):
    bindings = bindings or {}
    return legacy_projects(
        config, guild_id, channel_id_for=bindings.get, gitlab_routes=routes, github_channel_key="gitlab_activite"
    )


def test_no_legacy_project_without_webhook_configuration():
    assert legacy(settings()) == []


def test_legacy_github_project_reflects_env_and_never_its_secret():
    config = settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    [project] = legacy(config, bindings={"gitlab_activite": 5})
    assert (project.ref, project.provider, project.target, project.status, project.legacy) == (
        LEGACY_GITHUB, "github", "ElBastiano7553/game-project", "legacy_on", True
    )
    assert {r.category for r in project.routes} == {"code", "revues", "tickets", "ci"}
    assert all(r.channel_id == 5 for r in project.routes)
    assert GITHUB_SECRET not in repr(project)


def test_legacy_projects_belong_only_to_the_configured_guild():
    config = settings(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, GITLAB_WEBHOOK_SECRET=GITLAB_SECRET)
    assert len(legacy(config)) == 2
    assert legacy(config, guild_id=OTHER_GUILD) == []


def test_legacy_gitlab_project_maps_gitlab_routes_to_common_categories():
    config = settings(GITLAB_WEBHOOK_SECRET=GITLAB_SECRET, GITLAB_PROJECT_ID="1234")
    routes = {"push": "gitlab_activite", "merge_request": "gitlab_activite", "pipeline": "builds", "deployment": "builds"}
    [project] = legacy(config, routes=routes, bindings={"gitlab_activite": 5})
    assert project.ref == LEGACY_GITLAB and "1234" in project.target
    assert project.routes == (Route("code", 5), Route("revues", 5), Route("ci", None), Route("deploiements", None))
    [broken] = legacy(config, routes=None)
    assert broken.routes == () and "invalide" in broken.note


def test_invalid_legacy_configuration_is_shown_as_an_error():
    config = settings(GITHUB_WEBHOOK_SECRET="trop-court")
    [project] = legacy(config)
    assert project.status == "legacy_error"


def test_visible_routes_hide_private_and_missing_channels():
    routes = (Route("code", 1), Route("revues", 2), Route("ci", None), Route("versions", 3))
    shown, hidden = visible_routes(routes, lambda channel_id: channel_id == 1)
    assert shown == (Route("code", 1),) and hidden == 2


# --- /projet liste et /projet details ---------------------------------------------------------


class Member:
    def __init__(self, member_id: int = 7, *, admin: bool = False) -> None:
        self.id = member_id
        self.display_name = "Alice"
        self.guild_permissions = discord.Permissions(manage_guild=admin)
        self.roles: list = []

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other) -> bool:
        return getattr(other, "id", None) == self.id


class Response:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def is_done(self) -> bool:
        return bool(self.sent)

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class Interaction:
    def __init__(self, client, guild, user) -> None:
        self.client, self.guild, self.user = client, guild, user
        self.guild_id = getattr(guild, "id", None)
        self.response = Response()


def embed_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or "", embed.footer.text or ""]
    return " ".join(parts + [f"{f.name} {f.value}" for f in embed.fields])


@pytest.fixture
async def make_bot(db):
    bots: list[SaeBot] = []

    async def factory(**env) -> SaeBot:
        instance = SaeBot(settings(**env), db)
        for extension in EXTENSIONS:
            await instance.load_extension(extension)
        bots.append(instance)
        return instance

    yield factory
    for instance in bots:
        await instance.close()


async def run(bot, command: str, guild, user, **options) -> dict:
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, guild, user)
    callback = cog.list_projects if command == "liste" else cog.details
    await callback.callback(cog, interaction, **options)
    return interaction.response.sent[0]


async def test_list_is_empty_without_any_project(make_bot):
    bot = await make_bot()
    sent = await run(bot, "liste", FakeGuild(), Member())
    assert sent["ephemeral"] and "Aucun projet" in sent["embed"].description


async def test_list_shows_legacy_and_registered_projects_of_this_guild_only(make_bot):
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    bot.projects_repo.create(GUILD, "gitlab", "12", "Outils", created_by=7)
    bot.projects_repo.create(OTHER_GUILD, "github", "x/secret-ailleurs", "Projet d'un autre serveur", created_by=9)
    text = embed_text((await run(bot, "liste", FakeGuild(), Member(admin=True)))["embed"])
    assert "ElBastiano7553/game-project" in text and "Outils" in text and "historique" in text
    assert "secret-ailleurs" not in text
    assert GITHUB_SECRET not in text


async def test_list_is_paginated_beyond_one_page_and_never_loses_a_project(make_bot):
    from src.discord_bot.embeds import PROJECTS_PER_PAGE
    from src.discord_bot.meeting_ui import PagesView
    from src.services import text_limits as limits

    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    for number in range(30):  # + 1 projet historique = 31 projets, plus que les 25 champs d'un embed
        name = f"Projet {number:02d} " + "x" * 60  # noms longs : la taille totale compte aussi
        bot.projects_repo.create(GUILD, "github", f"equipe-{number}/" + "d" * 90, name, created_by=7)
    sent = await run(bot, "liste", FakeGuild(), Member(admin=True))
    view = sent["view"]
    assert isinstance(view, PagesView) and sent["embed"] is view.pages[0]
    assert len(view.pages) == 4 and "(1/4)" in view.pages[0].title
    for page in view.pages:
        assert len(page.fields) <= min(PROJECTS_PER_PAGE, limits.EMBED_FIELDS)
        assert len(page) <= limits.EMBED_TOTAL
    names = [field.name for page in view.pages for field in page.fields]
    assert len(names) == 31 == len(set(names))  # chaque projet exactement une fois
    assert sum("historique" in name for name in names) == 1


class ClickResponse(Response):
    def __init__(self) -> None:
        super().__init__()
        self.edited: list[dict] = []

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)


class Click(Interaction):
    """Clic sur un bouton : même forme que l'interaction d'une commande."""

    def __init__(self, client, user) -> None:
        super().__init__(client, None, user)
        self.response = ClickResponse()
        self.original_edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


async def _paged_list(make_bot, count: int = 25):
    bot = await make_bot()
    for number in range(count):
        bot.projects_repo.create(GUILD, "github", f"equipe/depot-{number}", f"Projet {number:02d}", created_by=7)
    return bot, (await run(bot, "liste", FakeGuild(), Member(admin=True)))["view"]


async def test_pagination_buttons_navigate_within_bounds(make_bot):
    bot, view = await _paged_list(make_bot)
    assert len(view.pages) == 3
    assert view.previous.disabled and not view.following.disabled
    click = Click(bot, Member())
    for _ in range(5):  # plus de clics que de pages : on reste sur la dernière
        if await view.interaction_check(click):
            await view.following.callback(click)
    assert view.index == 2 and view.following.disabled and not view.previous.disabled
    assert click.response.edited[-1]["embed"] is view.pages[2] and "(3/3)" in view.pages[2].title


async def test_pagination_buttons_are_reserved_to_the_author(make_bot):
    bot, view = await _paged_list(make_bot)
    intruder = Click(bot, Member(99, admin=True))  # même un administrateur
    assert await view.interaction_check(intruder) is False
    assert intruder.response.sent[0]["ephemeral"] and "quelqu'un d'autre" in intruder.response.sent[0]["content"]
    assert view.index == 0 and intruder.response.edited == []
    assert await view.interaction_check(Click(bot, Member())) is True


async def test_expired_pagination_removes_its_buttons_and_keeps_the_page(make_bot):
    bot, view = await _paged_list(make_bot)
    original = Click(bot, Member())
    view.original = original
    await view.on_timeout()
    assert original.original_edits == [{"view": None}]  # la page reste, seuls les boutons partent
    assert view.timeout is not None and view.timeout <= 14 * 60  # avant l'expiration du jeton Discord (15 min)


async def test_list_command_registers_the_original_interaction(make_bot):
    bot, view = await _paged_list(make_bot)
    assert view.original is not None


async def test_short_list_has_no_navigation_buttons(make_bot):
    bot = await make_bot()
    bot.projects_repo.create(GUILD, "gitlab", "12", "Outils", created_by=7)
    sent = await run(bot, "liste", FakeGuild(), Member(admin=True))
    assert "view" not in sent and len(sent["embed"].fields) == 1


async def test_details_show_members_only_the_channels_they_can_see(make_bot):
    bot = await make_bot()
    guild = FakeGuild()
    public, private = guild.add_channel("activité-git"), guild.add_channel("bot-logs")
    member = Member()
    private.overwrites[member] = discord.PermissionOverwrite(view_channel=False)
    project = bot.projects_repo.create(GUILD, "github", "a/b", "Jeu", created_by=1)
    bot.projects_repo.set_route(GUILD, project.id, "code", public.id)
    bot.projects_repo.set_route(GUILD, project.id, "ci", private.id)

    text = embed_text((await run(bot, "details", guild, member, projet=str(project.id)))["embed"])
    assert f"<#{public.id}>" in text and f"<#{private.id}>" not in text
    assert "1 salon(s) masqué(s)" in text
    assert "Ajouté par" not in text

    admin_text = embed_text((await run(bot, "details", guild, Member(8, admin=True), projet=str(project.id)))["embed"])
    assert f"<#{public.id}>" in admin_text and f"<#{private.id}>" in admin_text and "Ajouté par" in admin_text


async def test_details_of_a_legacy_project_never_show_secrets(make_bot):
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET, GITLAB_WEBHOOK_SECRET=GITLAB_SECRET)
    for ref in (LEGACY_GITHUB, LEGACY_GITLAB):
        sent = await run(bot, "details", FakeGuild(), Member(admin=True), projet=ref)
        text = embed_text(sent["embed"])
        assert "Configuration" in text and ".env" in text
        assert GITHUB_SECRET not in text and GITLAB_SECRET not in text


async def test_details_refuse_another_guilds_project_and_unknown_references(make_bot):
    bot = await make_bot()
    theirs = bot.projects_repo.create(OTHER_GUILD, "github", "x/y", "Leur projet", created_by=9)
    for ref in (str(theirs.id), "999", "env-github"):
        sent = await run(bot, "details", FakeGuild(), Member(admin=True), projet=ref)
        assert sent["ephemeral"] and "introuvable" in embed_text(sent["embed"]).casefold()


async def test_commands_are_refused_outside_the_configured_guild(make_bot):
    bot = await make_bot()
    other = FakeGuild()
    other.id = OTHER_GUILD
    bot.projects_repo.create(OTHER_GUILD, "github", "x/y", "Leur projet", created_by=9)
    sent = await run(bot, "liste", other, Member(admin=True))
    assert "pas encore ouvert" in embed_text(sent["embed"])


async def test_autocomplete_offers_only_this_guilds_projects(make_bot):
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    mine = bot.projects_repo.create(GUILD, "gitlab", "12", "Outils", created_by=7)
    bot.projects_repo.create(OTHER_GUILD, "gitlab", "13", "Outils ailleurs", created_by=9)
    cog = bot.get_cog("ProjectsCog")
    choices = await cog._project_autocomplete(Interaction(bot, FakeGuild(), Member(admin=True)), "")
    assert {c.value for c in choices} == {LEGACY_GITHUB, str(mine.id)}
