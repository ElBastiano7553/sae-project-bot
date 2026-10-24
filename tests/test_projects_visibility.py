"""Projets masqués aux membres : un membre ordinaire ne voit que les projets dont il voit au moins un salon.

Les administrateurs voient tout. Un projet masqué n'est révélé ni par la liste, ni par les détails,
ni par l'autocomplétion : il reçoit la même réponse qu'un projet qui n'existe pas.
"""

from __future__ import annotations

import discord
import pytest

from src.services.projects import LEGACY_GITHUB, ProjectView, Route, project_visible
from tests.fake_guild import FakeGuild
from tests.test_projects import (
    GITHUB_SECRET,
    GUILD,
    OTHER_GUILD,
    Interaction,
    Member,
    embed_text,
    make_bot,  # noqa: F401  (fixture)
    run,
)

# --- Règle (sans Discord) ----------------------------------------------------------------------


def view(*routes: Route) -> ProjectView:
    return ProjectView(ref="1", guild_id=GUILD, provider="github", name="Jeu", target="a/b", status="pending", routes=routes)


@pytest.mark.parametrize(
    ("routes", "visible"),
    [
        ((), False),  # aucun salon choisi
        ((Route("code", None),), False),  # salon historique non associé
        ((Route("code", 2),), False),  # salon privé
        ((Route("code", 2), Route("ci", 1)), True),  # un seul salon visible suffit
    ],
)
def test_a_project_is_visible_only_through_one_of_its_channels(routes, visible):
    assert project_visible(view(*routes), lambda channel_id: channel_id == 1) is visible


# --- Commandes ---------------------------------------------------------------------------------


@pytest.fixture
async def setup(make_bot):  # noqa: F811
    """Un projet avec un salon public, un projet dans un salon privé, un projet sans salon."""
    bot = await make_bot()
    guild = FakeGuild()
    member = Member()
    public, private = guild.add_channel("activité-git"), guild.add_channel("equipe-privee")
    private.overwrites[member] = discord.PermissionOverwrite(view_channel=False)
    repo = bot.projects_repo
    shown = repo.create(GUILD, "github", "equipe/jeu", "Jeu public", created_by=1)
    repo.set_route(GUILD, shown.id, "code", public.id)
    secret = repo.create(GUILD, "github", "equipe/prototype", "Prototype secret", created_by=1)
    repo.set_route(GUILD, secret.id, "code", private.id)
    bare = repo.create(GUILD, "gitlab", "12", "Projet sans salon", created_by=1)
    return bot, guild, member, shown, secret, bare


def hidden_names_absent(text: str) -> bool:
    return "Prototype" not in text and "prototype" not in text and "sans salon" not in text


async def test_member_list_shows_only_projects_with_a_visible_channel(setup):
    bot, guild, member, shown, secret, bare = setup
    text = embed_text((await run(bot, "liste", guild, member))["embed"])
    assert "Jeu public" in text and hidden_names_absent(text)
    assert "1 projet sur ce serveur" in text  # le nombre ne compte que les projets visibles


async def test_admin_list_shows_every_project(setup):
    bot, guild, member, shown, secret, bare = setup
    text = embed_text((await run(bot, "liste", guild, Member(8, admin=True)))["embed"])
    assert "Jeu public" in text and "Prototype secret" in text and "Projet sans salon" in text


async def test_member_who_sees_nothing_gets_the_same_page_as_an_empty_server(setup, make_bot):  # noqa: F811
    bot, guild, member, shown, secret, bare = setup
    bot.projects_repo.remove_route(GUILD, shown.id, "code")
    masked = (await run(bot, "liste", guild, member))["embed"]
    empty_bot = await make_bot()
    empty = (await run(empty_bot, "liste", FakeGuild(), member))["embed"]
    assert embed_text(masked) == embed_text(empty)
    assert "ajouter" not in embed_text(masked) and "administrat" not in embed_text(masked)


async def test_member_details_of_a_hidden_project_look_exactly_like_an_unknown_reference(setup):
    bot, guild, member, shown, secret, bare = setup
    unknown = embed_text((await run(bot, "details", guild, member, projet="999"))["embed"])
    for project in (secret, bare):
        sent = await run(bot, "details", guild, member, projet=str(project.id))
        assert sent["ephemeral"] and embed_text(sent["embed"]) == unknown
    assert "introuvable" in unknown.casefold()


async def test_member_details_of_a_visible_project_still_hide_private_channels(setup):
    bot, guild, member, shown, secret, bare = setup
    private = guild.by_name("equipe-privee")
    bot.projects_repo.set_route(GUILD, shown.id, "ci", private.id)
    text = embed_text((await run(bot, "details", guild, member, projet=str(shown.id)))["embed"])
    assert "Jeu public" in text and f"<#{private.id}>" not in text and "1 salon(s) masqué(s)" in text


async def test_admin_details_of_any_project(setup):
    bot, guild, member, shown, secret, bare = setup
    for project in (secret, bare):
        text = embed_text((await run(bot, "details", guild, Member(8, admin=True), projet=str(project.id)))["embed"])
        assert project.name in text


async def test_admin_role_name_gives_full_visibility(make_bot):  # noqa: F811
    bot = await make_bot(ADMIN_ROLE_NAME="Responsable")
    bot.projects_repo.create(GUILD, "github", "equipe/prototype", "Prototype secret", created_by=1)
    lead = Member(9)
    lead.roles = [type("Role", (), {"name": "responsable"})()]
    text = embed_text((await run(bot, "liste", FakeGuild(), lead))["embed"])
    assert "Prototype secret" in text


async def test_details_autocomplete_never_suggests_a_hidden_project(setup):
    bot, guild, member, shown, secret, bare = setup
    cog = bot.get_cog("ProjectsCog")
    for needle in ("", "proto", "sans", str(secret.id), str(bare.id)):
        choices = await cog._project_autocomplete(Interaction(bot, guild, member), needle)
        assert {c.value for c in choices} <= {str(shown.id)}
    admin_choices = await cog._project_autocomplete(Interaction(bot, guild, Member(8, admin=True)), "")
    assert {c.value for c in admin_choices} == {str(shown.id), str(secret.id), str(bare.id)}


async def test_modify_autocomplete_answers_nothing_to_members(setup):
    bot, guild, member, shown, secret, bare = setup
    cog = bot.get_cog("ProjectsCog")
    # Discord appelle l'autocomplétion même si la commande est refusée : rien, pas même les projets visibles.
    assert await cog._registered_autocomplete(Interaction(bot, guild, member), "") == []
    admin_choices = await cog._registered_autocomplete(Interaction(bot, guild, Member(8, admin=True)), "")
    assert len(admin_choices) == 3


async def test_deleted_channel_hides_the_project(setup):
    bot, guild, member, shown, secret, bare = setup
    bot.projects_repo.set_route(GUILD, shown.id, "code", 123456789)  # salon supprimé depuis
    text = embed_text((await run(bot, "liste", guild, member))["embed"])
    assert "Jeu public" not in text


async def test_legacy_project_follows_the_same_rule(make_bot):  # noqa: F811
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    guild = FakeGuild()
    member = Member()
    # Salon historique non associé : masqué aux membres, visible des administrateurs.
    assert "historique" not in embed_text((await run(bot, "liste", guild, member))["embed"])
    assert "historique" in embed_text((await run(bot, "liste", guild, Member(8, admin=True)))["embed"])
    channel = guild.add_channel("activité-git")
    bot.bindings_repo.replace_all({"gitlab_activite": channel.id})
    assert "historique" in embed_text((await run(bot, "liste", guild, member))["embed"])
    channel.overwrites[member] = discord.PermissionOverwrite(view_channel=False)
    sent = await run(bot, "details", guild, member, projet=LEGACY_GITHUB)
    assert "introuvable" in embed_text(sent["embed"]).casefold()


async def test_visibility_never_crosses_servers(setup):
    bot, guild, member, shown, secret, bare = setup
    elsewhere = FakeGuild()
    channel = elsewhere.add_channel("autre")
    theirs = bot.projects_repo.create(OTHER_GUILD, "github", "x/y", "Leur projet", created_by=9)
    bot.projects_repo.set_route(OTHER_GUILD, theirs.id, "code", channel.id)
    text = embed_text((await run(bot, "liste", guild, Member(8, admin=True)))["embed"])
    assert "Leur projet" not in text
    sent = await run(bot, "details", guild, member, projet=str(theirs.id))
    assert "introuvable" in embed_text(sent["embed"]).casefold()


async def test_member_pagination_counts_only_visible_projects(make_bot):  # noqa: F811
    bot = await make_bot()
    guild = FakeGuild()
    member = Member()
    public, private = guild.add_channel("public"), guild.add_channel("prive")
    private.overwrites[member] = discord.PermissionOverwrite(view_channel=False)
    for number in range(12):
        project = bot.projects_repo.create(GUILD, "github", f"equipe/visible-{number}", f"Visible {number:02d}", created_by=1)
        bot.projects_repo.set_route(GUILD, project.id, "code", public.id)
    for number in range(9):
        project = bot.projects_repo.create(GUILD, "github", f"equipe/cache-{number}", f"Caché {number:02d}", created_by=1)
        bot.projects_repo.set_route(GUILD, project.id, "code", private.id)
    sent = await run(bot, "liste", guild, member)
    pages = sent["view"].pages
    assert len(pages) == 2 and "(1/2)" in pages[0].title
    names = [f.name for page in pages for f in page.fields]
    assert len(names) == 12 and not any("Caché" in n for n in names)
    assert "12 projets" in pages[0].description
