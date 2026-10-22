"""Centre d'aide (/help, /reunion help) : contenu fidèle aux commandes réelles, navigation, limites Discord."""

from __future__ import annotations

import re

import discord
import pytest

from src.config import Settings
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.discord_bot.help_ui import HelpView, collect_commands, render_pages
from src.services import text_limits as limits
from src.services.help import (
    GUIDE_PAGES,
    HELP_ENTRIES,
    MEETING_PAGES,
    CommandInfo,
    ParamInfo,
    check_example,
    mentioned_commands,
    visible_sections,
)


@pytest.fixture
async def bot(db):
    instance = SaeBot(Settings.from_env({"DISCORD_GUILD_ID": "42"}, require_token=False), db)
    for extension in EXTENSIONS:
        await instance.load_extension(extension)
    yield instance
    await instance.close()


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self._done = True

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)
        self._done = True


class FakeUser:
    def __init__(self, user_id: int = 7) -> None:
        self.id = user_id
        self.display_name = "Alice"


class FakeInteraction:
    def __init__(self, client, user_id: int = 7, values=None) -> None:
        self.client = client
        self.user = FakeUser(user_id)
        self.guild = None
        self.response = FakeResponse()
        self.data = {"values": values} if values else {}
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


def page_text(page) -> str:
    texts = [page.intro, *[f"{s.title} {s.body}" for s in page.sections]]
    return "\n".join(texts)


def embed_text(embed: discord.Embed) -> str:
    return " ".join([embed.title or "", embed.description or "", *[f"{f.name} {f.value}" for f in embed.fields]])


def groups(bot) -> set[str]:
    return {name.split()[0] for name in (c.name for c in collect_commands(bot.tree)) if " " in name}


# --- Source de vérité ----------------------------------------------------------------------


async def test_commands_are_read_from_discord_with_their_parameters(bot):
    commands = {c.name: c for c in collect_commands(bot.tree)}
    creer = commands["reunion creer"]
    assert creer.required == ["titre"]
    assert {"etapes", "participants", "duree", "responsable"} <= set(creer.optional)
    assert commands["refonte lancer"].admin and commands["refonte retour"].admin and not commands["help"].admin
    assert {"help", "reunion help", "refonte lancer", "refonte retour", "refonte journal"} <= set(commands)


async def test_every_example_matches_a_real_command_and_its_options(bot):
    commands = collect_commands(bot.tree)
    problems = [p for entry in HELP_ENTRIES if entry.example for p in check_example(entry.example, commands)]
    # Exemples cités dans les pages de guide : `\/commande option:valeur …`
    for page in (*GUIDE_PAGES, *MEETING_PAGES):
        for example in re.findall(r"`(/[a-z][^`]*:[^`]*)`", page_text(page)):
            if "…" in example or "<" in example:
                continue  # gabarit (« titre:… », « reunion:<numéro> »), pas un exemple complet
            problems += check_example(example, commands)
    assert problems == []


async def test_guide_pages_only_cite_existing_commands(bot):
    existing = {c.name for c in collect_commands(bot.tree)} | groups(bot)
    for page in (*GUIDE_PAGES, *MEETING_PAGES):
        cited = mentioned_commands(page_text(page), groups(bot))
        assert cited <= existing, f"{page.key} cite des commandes inexistantes : {cited - existing}"


async def test_member_pages_never_cite_admin_commands(bot):
    commands = collect_commands(bot.tree)
    admin_commands = {c.name for c in commands if c.admin} | {c.name.split()[0] for c in commands if c.admin}
    for page in (*GUIDE_PAGES, *MEETING_PAGES):
        text = "\n".join(f"{s.title} {s.body}" for s in visible_sections(page, admin=False)) + page.intro
        assert not (mentioned_commands(text, groups(bot)) & admin_commands), page.key


def test_planned_features_are_never_presented_as_available():
    everything = "\n".join(page_text(p) for p in (*GUIDE_PAGES, *MEETING_PAGES))
    for planned in ("/idee", "/sondage", "/debat", "/classement", "/ressources", "/roles", "/reunion classer"):
        assert planned not in everything
    organisation = next(p for p in GUIDE_PAGES if p.key == "organisation")
    unavailable = next(s for s in organisation.sections if s.title == "Pas encore disponible").body
    for feature in ("tier lists", "sondages", "auto-attribution", "Proposer une décision"):
        assert feature in unavailable


def test_meeting_help_distinguishes_official_decisions_from_proposals():
    page = next(p for p in MEETING_PAGES if p.key == "informations")
    sections = {s.title: s.body for s in page.sections}
    assert "responsable et aux administrateurs" in sections["Décision officielle"]
    assert "pas encore" in sections["Proposer une décision"]


def test_check_example_reports_unknown_and_missing_options():
    commands = [CommandInfo("reunion creer", "", (ParamInfo("titre", "", True), ParamInfo("duree", "", False)))]
    assert check_example("/reunion creer titre:A duree:30", commands) == []
    assert check_example("/reunion creer duree:30", commands) == ["« /reunion creer duree:30 » : option obligatoire « titre » absente"]
    assert "inconnue" in check_example("/reunion creer titre:A salle:B", commands)[0]
    assert "commande inconnue" in check_example("/reunion organiser", commands)[0]


def test_mentioned_commands_ignores_paths_and_urls():
    text = "Voir `config/server.toml`, logs/bot.log, https://x.y/z puis `/status` et /reunion creer."
    assert mentioned_commands(text, {"reunion"}) == {"status", "reunion creer"}


# --- Rendu et limites Discord -------------------------------------------------------------


def test_texts_fit_discord_limits_without_truncation():
    for page in (*GUIDE_PAGES, *MEETING_PAGES):
        assert len(page.title) <= 80 and len(page.summary) <= 100
        assert len(page.intro) <= limits.EMBED_DESCRIPTION
        for section in page.sections:
            assert len(section.body) <= limits.FIELD_VALUE, (page.key, section.title)
            assert len(section.title) <= limits.FIELD_NAME


@pytest.mark.parametrize("admin", [False, True])
async def test_every_page_renders_within_limits(bot, admin):
    commands = collect_commands(bot.tree)
    for pages, heading in ((GUIDE_PAGES, "Aide de Samae"), (MEETING_PAGES, "Aide des réunions")):
        rendered = render_pages(pages, commands=commands, admin=admin, heading=heading)
        assert {p.key for p in rendered} == {p.key for p in pages}
        for page in rendered:
            assert len(page.embed) <= limits.EMBED_TOTAL
            assert len(page.embed.fields) <= limits.EMBED_FIELDS
            assert all(len(f.value) <= limits.FIELD_VALUE for f in page.embed.fields)
            assert not page.embed.description or "…" not in page.embed.description[-2:]


async def test_commands_page_lists_every_command_for_admins_and_hides_admin_ones(bot):
    commands = collect_commands(bot.tree)
    for admin in (True, False):
        rendered = render_pages(GUIDE_PAGES, commands=commands, admin=admin, heading="Aide")
        text = " ".join(embed_text(p.embed) for p in rendered if p.key == "commandes")
        for command in commands:
            assert (f"`/{command.name}`" in text) == (admin or not command.admin), command.name


async def test_help_command_opens_requested_section(bot):
    general = bot.get_cog("GeneralCog")
    interaction = FakeInteraction(bot)
    choice = discord.app_commands.Choice(name="Dépannage", value="depannage")
    await general.help.callback(general, interaction, choice)
    sent = interaction.response.sent[0]
    assert sent["ephemeral"] and sent["embed"].title == "Aide de Samae · Dépannage"
    assert sent["view"].pages[sent["view"].index].key == "depannage"


# --- Navigation ----------------------------------------------------------------------------


def make_view(bot, admin=False) -> HelpView:
    return HelpView(render_pages(GUIDE_PAGES, commands=collect_commands(bot.tree), admin=admin, heading="Aide"), 7)


def buttons(view: HelpView) -> dict[str, discord.ui.Button]:
    return {item.label: item for item in view.children if isinstance(item, discord.ui.Button)}


async def test_navigation_previous_next_home_and_menu(bot):
    view = make_view(bot)
    total = len(view.pages)
    assert buttons(view)["Précédent"].disabled and buttons(view)["Accueil"].disabled
    assert f"1/{total}" in buttons(view)

    interaction = FakeInteraction(bot)
    await buttons(view)["Suivant"].callback(interaction)
    assert view.index == 1 and interaction.response.edited[0]["embed"] is view.pages[1].embed
    assert not buttons(view)["Précédent"].disabled

    await buttons(view)["Accueil"].callback(FakeInteraction(bot))
    assert view.index == 0

    select = next(item for item in view.children if isinstance(item, discord.ui.Select))
    assert [o.value for o in select.options] == [p.key for p in GUIDE_PAGES]
    await view._on_select(FakeInteraction(bot, values=["depannage"]))
    assert view.pages[view.index].key == "depannage"

    view.go_to(total - 1)
    assert buttons(view)["Suivant"].disabled


async def test_only_the_author_can_use_the_buttons(bot):
    view = make_view(bot)
    intruder = FakeInteraction(bot, user_id=99)
    assert await view.interaction_check(intruder) is False
    assert intruder.response.sent[0]["ephemeral"]
    assert await view.interaction_check(FakeInteraction(bot)) is True


async def test_expired_help_keeps_the_page_and_removes_buttons(bot):
    view = make_view(bot)
    original = FakeInteraction(bot)
    view.original = original
    await view.on_timeout()
    assert original.edits == [{"view": None}]


async def test_navigation_buttons_never_trigger_admin_actions(bot):
    view = make_view(bot, admin=True)
    before = bot.admin_log.recent()
    for _ in range(len(view.pages)):
        await buttons(view)["Suivant"].callback(FakeInteraction(bot)) if not buttons(view)["Suivant"].disabled else None
    assert bot.admin_log.recent() == before
    assert bot.bindings_repo.get_all() == {}
