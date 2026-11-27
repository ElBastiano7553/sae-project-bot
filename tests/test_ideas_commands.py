"""Commandes /idee, /tierlist, /debat, /suivi et /samae avec un faux Discord en mémoire (aucun réseau)."""

from __future__ import annotations

import discord
import pytest

from src.config import Settings
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.discord_bot.ideas_ui import BallotView, IdeaModal, OptionVoteView
from src.discord_bot.views import ConfirmView
from src.services.meetings import Actor
from tests.test_meeting_commands import FakeChannel, FakeInteraction, FakeUser, text_of
from tests.tracker_fixture import build_workbook

GUILD_ID = 42
CHANNEL_ID = 100
ADMIN = FakeUser(111111111111111111, "Romain")
BOB = FakeUser(222222222222222222, "Bob")
CHLOE = FakeUser(333333333333333333, "Chloé")
ADMINS = {ADMIN.id}


@pytest.fixture
async def env(tmp_path):
    from src.database import Database

    db = Database(tmp_path / "sae_bot.sqlite3")
    settings = Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID), "DATABASE_PATH": str(tmp_path / "sae_bot.sqlite3")},
                                 require_token=False)
    bot = SaeBot(settings, db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    channel = FakeChannel(CHANNEL_ID)
    for name in ("IdeasCog", "TrackerCog", "AssistantCog"):
        cog = bot.get_cog(name)
        cog._actor = (lambda c: (lambda *args: Actor(args[0].user.id if hasattr(args[0], "user") else args[0].id,
                                                     (args[0].user if hasattr(args[0], "user") else args[0]).display_name,
                                                     (args[0].user.id if hasattr(args[0], "user") else args[0].id) in ADMINS,
                                                     GUILD_ID)))(cog)
    yield bot, channel
    await bot.close()
    db.close()


def interaction(bot, user, channel):
    i = FakeInteraction(bot, user, channel)
    i.namespace = type("NS", (), {})()

    async def send_modal(modal):
        i.response.sent.append({"modal": modal})
        i.response._done = True

    i.response.send_modal = send_modal
    return i


async def submit_idea(bot, channel, user, name, genre="Plateforme", style="Solo", description=""):
    cog = bot.get_cog("IdeasCog")
    i = interaction(bot, user, channel)
    await cog.idea_propose.callback(cog, i)
    modal = i.response.sent[0]["modal"]
    assert isinstance(modal, IdeaModal)
    submit = interaction(bot, user, channel)
    await modal._on_submit(submit, {"name": name, "play_style": style, "genre": genre, "description": description})
    return submit


async def test_idea_form_creates_idea_and_warns_about_similar_ones(env):
    bot, channel = env
    first = await submit_idea(bot, channel, BOB, "Roguelike spatial", genre="Roguelike", description="vaisseau et donjons")
    assert "Idée #1 ajoutée" in text_of(first.response.sent[0]["embeds"][0])
    second = await submit_idea(bot, channel, CHLOE, "Roguelike dans l'espace", genre="Roguelike", description="donjons à bord d'un vaisseau")
    assert "Idées proches" in text_of(second.response.sent[0]["embeds"][1])
    for n in range(4):
        await submit_idea(bot, channel, BOB, f"Idée {n}", genre=f"G{n}")
    refused = await submit_idea(bot, channel, BOB, "Sixième", genre="G9")
    assert "déjà 5 idées" in text_of(refused.response.sent[0]["embed"])


async def test_tierlist_full_flow_keeps_votes_hidden(env):
    bot, channel = env
    await submit_idea(bot, channel, BOB, "Alpha", genre="A")
    await submit_idea(bot, channel, CHLOE, "Beta", genre="B")
    cog = bot.get_cog("IdeasCog")
    create = interaction(bot, ADMIN, channel)
    await cog.tl_create.callback(cog, create, titre="Concepts", idees=None, reunion=None)
    assert create.response.sent[0]["allowed_mentions"] is not None
    tl_id = bot.tierlists_repo.list(GUILD_ID)[0].id

    vote = interaction(bot, BOB, channel)
    await cog.tl_vote.callback(cog, vote, tierlist=tl_id)
    view = vote.response.sent[0]["view"]
    assert isinstance(view, BallotView) and len(view.items) == 2
    validate_button = [c for c in view.children if isinstance(c, discord.ui.Button) and c.label.startswith("Valider")][0]
    assert validate_button.disabled  # tant que tout n'est pas classé
    view.ballot = view._on_vote(view.items[0].id, "S")
    view.ballot = view._on_vote(view.items[1].id, "F")
    view._build()
    validate_button = [c for c in view.children if isinstance(c, discord.ui.Button) and c.label.startswith("Valider")][0]
    assert not validate_button.disabled
    click = interaction(bot, BOB, channel)
    await validate_button.callback(click)
    assert click.response.edited[0]["view"] is None

    rank = interaction(bot, CHLOE, channel)
    await cog.tl_rank.callback(cog, rank, tierlist=tl_id, idee=view.items[0].id, rang=discord.app_commands.Choice(name="A", value="A"))
    assert "classée **A**" in text_of(rank.response.sent[0]["embeds"][0])

    progress = interaction(bot, CHLOE, channel)
    await cog.tl_progress.callback(cog, progress, tierlist=tl_id)
    shown = text_of(progress.response.sent[0]["embed"])
    assert "1 bulletin(s) validé(s), 1 en cours" in shown and "→ **S**" not in shown

    close = interaction(bot, ADMIN, channel)
    await cog.tl_close.callback(cog, close, tierlist=tl_id)
    confirm = close.response.sent[0]["view"]
    assert isinstance(confirm, ConfirmView)
    await confirm._on_confirm(close)
    published = [text_of(m.embed) for m in channel.messages.values() if m.embed and "Résultats" in (m.embed.title or "")]
    assert published and "Alpha" in published[0] and "1 non validé(s), non comptés" in published[0]
    assert str(BOB.id) not in published[0] and "Bob" not in published[0]  # aucun votant nommé


async def test_debate_confirmation_requires_confirm_view(env):
    bot, channel = env
    await submit_idea(bot, channel, BOB, "Clivante")
    cog = bot.get_cog("IdeasCog")
    opened = interaction(bot, CHLOE, channel)
    await cog.debate_open.callback(cog, opened, sujet=None, idee=1, tierlist=None, reunion=None)
    debate_id = bot.debates_repo.list(GUILD_ID)[0].id
    tiebreak = interaction(bot, ADMIN, channel)
    await cog.debate_tiebreak.callback(cog, tiebreak, debat=debate_id, options="Garder ; Abandonner")
    vote = interaction(bot, BOB, channel)
    await cog.debate_vote.callback(cog, vote, debat=debate_id)
    assert isinstance(vote.response.sent[0]["view"], OptionVoteView)
    propose = interaction(bot, BOB, channel)
    await cog.debate_propose.callback(cog, propose, debat=debate_id, decision="On garde", idee_statut=None)
    assert bot.debates_repo.get(debate_id).status == "proposed"
    confirm = interaction(bot, ADMIN, channel)
    await cog.debate_confirm.callback(cog, confirm, debat=debate_id)
    assert bot.debates_repo.get(debate_id).status == "proposed"  # rien tant que le bouton n'est pas cliqué
    await confirm.response.sent[0]["view"]._on_confirm(confirm)
    assert bot.debates_repo.get(debate_id).decision == "On garde"


class FakeAttachment:
    def __init__(self, data: bytes, filename: str = "GAME DESIGN DATABASE — LUNASTEH.xlsx") -> None:
        self._data = data
        self.filename = filename
        self.size = len(data)

    async def read(self) -> bytes:
        return self._data


async def test_tracker_import_preview_then_confirm_then_queries(env):
    bot, channel = env
    cog = bot.get_cog("TrackerCog")
    start = interaction(bot, ADMIN, channel)
    await cog.import_workbook.callback(cog, start, fichier=FakeAttachment(build_workbook()))
    preview = start.followup.sent[0]
    assert "11 nouvelle(s) ligne(s)" in text_of(preview["embed"]) and bot.tracker.info() is None
    await preview["view"]._on_confirm(start)
    assert bot.tracker.info() is not None and "Import terminé" in start.edits[-1]["content"]

    progress = interaction(bot, BOB, channel)
    await cog.progress.callback(cog, progress)
    assert "1/3" in text_of(progress.response.sent[0]["embed"])

    add = interaction(bot, BOB, channel)
    await cog.add.callback(cog, add, onglet=discord.app_commands.Choice(name="DECISIONS", value="DECISIONS"),
                           texte="Résolution 720p", description=None, statut=None)
    assert "validation humaine" in text_of(add.response.sent[0]["embed"])

    wrong = interaction(bot, BOB, channel)
    await cog.import_workbook.callback(cog, wrong, fichier=FakeAttachment(b"pas un excel", filename="notes.txt"))
    assert ".xlsx" in text_of(wrong.response.sent[0]["embed"])


async def test_samae_command_answers_and_offers_prefilled_form(env):
    bot, channel = env
    cog = bot.get_cog("AssistantCog")
    ask = interaction(bot, BOB, channel)
    await cog.samae.callback(cog, ask, demande="Quelles idées sont encore en attente ?")
    assert "Idées en attente" in ask.followup.sent[0]["embed"].title
    add = interaction(bot, BOB, channel)
    await cog.samae.callback(cog, add, demande="Ajoute cette idée : Boss volant")
    view = add.followup.sent[0]["view"]
    click = interaction(bot, BOB, channel)
    await view.children[0].callback(click)
    modal = click.response.sent[0]["modal"]
    assert isinstance(modal, IdeaModal) and modal.name.default == "Boss volant"
    assert bot.ideas_repo.list(GUILD_ID) == []


async def test_mentions_are_ignored_unless_enabled(env):
    bot, channel = env
    cog = bot.get_cog("AssistantCog")

    class Message:
        author = BOB
        guild = type("G", (), {"id": GUILD_ID})()
        mentions: list = []
        content = "bilan ?"

    await cog.on_message(Message())  # désactivé par défaut : aucune exception, aucune réponse
    assert not bot.settings.assistant_on_mention
