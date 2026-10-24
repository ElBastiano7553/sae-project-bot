"""Multi-projets, étape B2 : salons et filtres par projet (/projet modifier, administrateurs)."""

from __future__ import annotations

import discord
import pytest

from src.services.projects import CLEAR_FILTER, ProjectError, Route, describe_filters, update_filters
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

# --- Filtres (sans Discord) -------------------------------------------------------------------


def test_branches_are_set_deduplicated_and_cleared():
    filters = update_filters("code", {}, branches=" main, develop ,main, feature/* ")
    assert filters == {"branches": ["main", "develop", "feature/*"]}
    assert update_filters("code", filters, branches=CLEAR_FILTER) == {}
    assert update_filters("ci", {"statuts": ["echoue"]}, branches="main") == {"statuts": ["echoue"], "branches": ["main"]}


@pytest.mark.parametrize("branches", ["", " , ", "main develop", "a" * 101, ", ".join(f"b{i}" for i in range(21)), "ma$in"])
def test_invalid_branches_are_refused(branches):
    with pytest.raises(ProjectError):
        update_filters("code", {}, branches=branches)


def test_branches_only_for_code_and_ci():
    with pytest.raises(ProjectError, match="branche"):
        update_filters("tickets", {}, branches="main")


def test_actions_and_statuses_use_the_common_vocabulary():
    assert update_filters("revues", {}, values="Fusion, ouverture, fusion") == {"actions": ["ouverture", "fusion"]}
    assert update_filters("deploiements", {}, values="echoue") == {"statuts": ["echoue"]}
    assert update_filters("revues", {"actions": ["fusion"]}, values=CLEAR_FILTER) == {}
    with pytest.raises(ProjectError, match="Valeurs possibles"):
        update_filters("ci", {}, values="reussi, explose")
    with pytest.raises(ProjectError, match="pas de filtre"):
        update_filters("versions", {}, values="ouverture")


def test_untouched_options_keep_the_current_filters():
    current = {"branches": ["main"], "statuts": ["echoue"]}
    assert update_filters("ci", current) == current


def test_describe_filters():
    assert describe_filters({}) == "tout"
    assert describe_filters({"branches": ["main"], "statuts": ["echoue", "annule"]}) == "branches : `main` · statuts : echoue, annule"


# --- /projet modifier -------------------------------------------------------------------------


async def modify(bot, guild, user=None, **options) -> dict:
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, guild, user or Member(1, admin=True))
    category = options.pop("categorie", "code")
    choice = discord.app_commands.Choice(name=category, value=category)
    await cog.modify.callback(cog, interaction, options.pop("projet"), choice, **options)
    return interaction.response.sent[0]


@pytest.fixture
async def setup(make_bot):  # noqa: F811
    bot = await make_bot(GITHUB_WEBHOOK_SECRET=GITHUB_SECRET)
    guild = FakeGuild()
    channel = guild.add_channel("activité-git")
    project = bot.projects_repo.create(GUILD, "github", "equipe/jeu", "Jeu", created_by=1)
    return bot, guild, channel, project


async def test_members_are_refused_by_the_bot_side_check(setup):
    bot, guild, channel, project = setup
    cog = bot.get_cog("ProjectsCog")
    [check] = cog.modify.checks
    assert await check(Interaction(bot, guild, Member(2))) is False
    assert bot.projects_repo.routes(GUILD, project.id) == ()


async def test_admin_sets_channel_then_filters_and_the_action_is_logged(setup):
    bot, guild, channel, project = setup
    sent = await modify(bot, guild, projet=str(project.id), salon=channel)
    assert sent["ephemeral"] and f"<#{channel.id}>" in embed_text(sent["embed"])
    assert bot.projects_repo.routes(GUILD, project.id) == (Route("code", channel.id, {}),)

    await modify(bot, guild, projet=str(project.id), branches="main, develop")
    assert bot.projects_repo.routes(GUILD, project.id) == (Route("code", channel.id, {"branches": ["main", "develop"]}),)

    sent = await modify(bot, guild, projet=str(project.id), branches=CLEAR_FILTER)
    assert bot.projects_repo.routes(GUILD, project.id) == (Route("code", channel.id, {}),)
    text = embed_text(sent["embed"])
    assert "Avant" in text and "main" in text and "Maintenant" in text

    actions = bot.admin_log.recent()
    assert [a.action for a in actions] == ["projet modifier"] * 3 and actions[0].target == f"{project.id}:code"


async def test_filters_without_any_channel_are_refused(setup):
    bot, guild, channel, project = setup
    sent = await modify(bot, guild, projet=str(project.id), categorie="ci", filtre="echoue")
    assert "salon" in embed_text(sent["embed"]).casefold()
    assert bot.projects_repo.routes(GUILD, project.id) == ()


async def test_invalid_filter_changes_nothing(setup):
    bot, guild, channel, project = setup
    await modify(bot, guild, projet=str(project.id), categorie="revues", salon=channel, filtre="fusion")
    sent = await modify(bot, guild, projet=str(project.id), categorie="revues", filtre="supprimer-tout")
    assert "Valeurs possibles" in embed_text(sent["embed"])
    assert bot.projects_repo.routes(GUILD, project.id) == (Route("revues", channel.id, {"actions": ["fusion"]}),)


async def test_channel_of_another_server_or_not_textual_is_refused(setup):
    bot, guild, channel, project = setup
    elsewhere = FakeGuild()
    foreign = elsewhere.add_channel("autre-serveur")
    voice = guild.add_channel("vocal", kind="voice")
    for wrong in (foreign, voice):
        sent = await modify(bot, guild, projet=str(project.id), salon=wrong)
        assert sent["embed"].title and "refusé" in sent["embed"].title.casefold()
    assert bot.projects_repo.routes(GUILD, project.id) == ()


async def test_channel_where_samae_cannot_post_is_refused(make_bot):  # noqa: F811
    bot = await make_bot()
    guild = FakeGuild(bot_permissions={"view_channel", "embed_links"})
    channel = guild.add_channel("muet")
    project = bot.projects_repo.create(GUILD, "gitlab", "12", "Outils", created_by=1)
    sent = await modify(bot, guild, projet=str(project.id), salon=channel)
    assert "Envoyer des messages" in embed_text(sent["embed"])
    assert bot.projects_repo.routes(GUILD, project.id) == ()


async def test_remove_a_category(setup):
    bot, guild, channel, project = setup
    await modify(bot, guild, projet=str(project.id), salon=channel)
    refused = await modify(bot, guild, projet=str(project.id), retirer=True, branches="main")
    assert "s'utilise seul" in embed_text(refused["embed"])
    sent = await modify(bot, guild, projet=str(project.id), retirer=True)
    assert "plus publiée" in sent["embed"].title
    assert bot.projects_repo.routes(GUILD, project.id) == ()
    again = await modify(bot, guild, projet=str(project.id), retirer=True)
    assert "rien à retirer" in embed_text(again["embed"])


async def test_no_option_at_all_is_refused(setup):
    bot, guild, channel, project = setup
    sent = await modify(bot, guild, projet=str(project.id))
    assert "au moins" in embed_text(sent["embed"])


async def test_legacy_projects_are_read_only_and_env_configuration_is_untouched(setup):
    bot, guild, channel, project = setup
    bot.bindings_repo.replace_all({"gitlab_activite": channel.id})
    sent = await modify(bot, guild, projet="env-github", salon=guild.add_channel("autre"))
    assert "historique" in sent["embed"].title.casefold() and ".env" in embed_text(sent["embed"])
    assert bot.bindings_repo.get_all() == {"gitlab_activite": channel.id}
    assert bot.settings.github_webhooks_enabled  # le webhook historique reste actif
    assert bot.admin_log.recent() == []


async def test_another_servers_project_cannot_be_modified(setup):
    bot, guild, channel, project = setup
    theirs = bot.projects_repo.create(OTHER_GUILD, "github", "x/y", "Leur projet", created_by=9)
    sent = await modify(bot, guild, projet=str(theirs.id), salon=channel)
    assert "introuvable" in embed_text(sent["embed"]).casefold()
    assert bot.projects_repo.routes(OTHER_GUILD, theirs.id) == ()


async def test_details_show_the_filters(setup):
    bot, guild, channel, project = setup
    await modify(bot, guild, projet=str(project.id), categorie="ci", salon=channel, filtre="echoue", branches="main")
    cog = bot.get_cog("ProjectsCog")
    interaction = Interaction(bot, guild, Member())
    await cog.details.callback(cog, interaction, projet=str(project.id))
    assert "statuts : echoue" in embed_text(interaction.response.sent[0]["embed"])


async def test_modify_autocomplete_offers_only_registered_projects(setup):
    bot, guild, channel, project = setup
    cog = bot.get_cog("ProjectsCog")
    choices = await cog._registered_autocomplete(Interaction(bot, guild, Member(1, admin=True)), "")
    assert [c.value for c in choices] == [str(project.id)]
