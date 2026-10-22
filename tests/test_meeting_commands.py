"""Commandes /reunion et boutons du panneau, avec un faux Discord en mémoire (aucun réseau)."""

from __future__ import annotations

import asyncio
import itertools
import re

import discord
import pytest

from src.config import Settings
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.discord_bot.meeting_ui import BUTTON_TEMPLATE, MeetingButton, panel_embed, panel_view, report_embeds
from src.services import text_limits as limits
from src.services.meetings import DECISION, FINISHED, RESERVE, RUNNING, Actor

GUILD_ID = 42
CHANNEL_ID = 100
DECISIONS_CHANNEL_ID = 200
LEADER_ID, MEMBER_ID, OUTSIDER_ID = 111111111111111111, 222222222222222222, 333333333333333333
_ids = itertools.count(5000)


class _Response:
    status = 404
    reason = "Not Found"


def not_found() -> discord.NotFound:
    return discord.NotFound(_Response(), "Unknown Message")


def forbidden() -> discord.Forbidden:
    response = _Response()
    response.status, response.reason = 403, "Forbidden"
    return discord.Forbidden(response, "Missing Permissions")


class FakeMessage:
    def __init__(self, channel, embed=None, view=None) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.embed = embed
        self.view = view


class FakePartial:
    def __init__(self, channel, message_id) -> None:
        self.channel = channel
        self.id = message_id

    async def edit(self, **kwargs):
        if self.id not in self.channel.messages:
            raise not_found()
        self.channel.edits.append((self.id, kwargs))
        self.channel.messages[self.id].embed = kwargs.get("embed")

    async def delete(self):
        self.channel.messages.pop(self.id, None)


class FakeChannel:
    def __init__(self, channel_id: int, *, can_send: bool = True) -> None:
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.edits: list = []
        self.can_send = can_send

    async def send(self, embed=None, view=None, **kwargs):
        if not self.can_send:
            raise forbidden()
        assert kwargs.get("allowed_mentions") is not None, "aucun message public sans protection des mentions"
        message = FakeMessage(self, embed, view)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise not_found()
        return self.messages[message_id]


class FakeUser:
    bot = False

    def __init__(self, user_id: int, name: str) -> None:
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"

    def __str__(self) -> str:
        return self.display_name


class FakeInteractionResponse:
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

    async def defer(self, **kwargs):
        self._done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeGuild:
    id = GUILD_ID

    def get_member(self, user_id):
        return None


class FakeInteraction:
    def __init__(self, client, user: FakeUser, channel: FakeChannel, message=None) -> None:
        self.client = client
        self.user = user
        self.guild = FakeGuild()
        self.channel = channel
        self.channel_id = channel.id
        self.message = message
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.command = None
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


@pytest.fixture
async def env(db):
    bot = SaeBot(Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID)}, require_token=False), db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    channels = {CHANNEL_ID: FakeChannel(CHANNEL_ID), DECISIONS_CHANNEL_ID: FakeChannel(DECISIONS_CHANNEL_ID)}
    bot.get_channel = channels.get  # type: ignore[method-assign]
    bot.bindings_repo.replace_all({"decisions": DECISIONS_CHANNEL_ID})
    yield bot, bot.get_cog("MeetingsCog"), channels
    await bot.close()


LEADER = FakeUser(LEADER_ID, "Alice")
MEMBER = FakeUser(MEMBER_ID, "Bob")
OUTSIDER = FakeUser(OUTSIDER_ID, "Chloé")


def text_of(embed: discord.Embed) -> str:
    return " ".join([embed.title or "", embed.description or "", *[f"{f.name} {f.value}" for f in embed.fields]])


def button_ids(view: discord.ui.View) -> list[str]:
    return [item.item.custom_id if isinstance(item, MeetingButton) else item.custom_id for item in view.children]


async def create(bot, cog, channels, **options):
    interaction = FakeInteraction(bot, LEADER, channels[CHANNEL_ID])
    params = {
        "titre": "Point sprint 3",
        "etapes": "Bilan ; Démo ; Planning",
        "participants": f"<@{MEMBER_ID}>",
        "duree": 45,
        **options,
    }
    await cog.creer.callback(cog, interaction, **params)
    return interaction


async def test_create_publishes_one_panel_and_answers_privately(env):
    bot, cog, channels = env
    interaction = await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    panel = channels[CHANNEL_ID].messages[meeting.panel_message_id]
    assert len(channels[CHANNEL_ID].messages) == 1
    assert "Point sprint 3" in panel.embed.title
    text = text_of(panel.embed)
    assert f"<@{LEADER_ID}>" in text and f"<@{MEMBER_ID}>" in text and "Bilan" in text
    assert interaction.followup.sent[0]["ephemeral"]
    assert "Voir le panneau" in interaction.followup.sent[0]["embed"].description
    assert [cid.split(":")[3] for cid in button_ids(panel.view)] == ["join", "leave", "report", "start", "cancel"]


async def test_create_without_permission_keeps_the_meeting(env):
    bot, cog, channels = env
    channels[CHANNEL_ID].can_send = False
    interaction = await create(bot, cog, channels)
    assert bot.meetings.repo.list()[0].panel_message_id is None
    embed = interaction.followup.sent[0]["embed"]
    assert "Panneau non publié" in embed.title and "Traceback" not in embed.description


async def test_buttons_check_permissions_server_side(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    panel = channels[CHANNEL_ID].messages[meeting.panel_message_id]
    # Un membre qui n'est pas responsable clique sur « Démarrer » : refusé, rien ne change.
    click = FakeInteraction(bot, MEMBER, channels[CHANNEL_ID], panel)
    await cog.handle_button(click, meeting.id, "start", meeting.revision)
    assert "Accès refusé" in click.response.sent[0]["embed"].title and click.response.sent[0]["ephemeral"]
    assert bot.meetings.get(meeting.id).meeting.status == "planned"
    # Le responsable démarre : le même message est modifié, aucun nouveau message.
    click = FakeInteraction(bot, LEADER, channels[CHANNEL_ID], panel)
    await cog.handle_button(click, meeting.id, "start", meeting.revision)
    assert bot.meetings.get(meeting.id).meeting.status == RUNNING
    assert "En cours" in text_of(click.response.edited[0]["embed"])
    assert len(channels[CHANNEL_ID].messages) == 1


async def test_double_click_on_stale_panel_does_not_skip_a_step(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    detail = bot.meetings.start(meeting.id, Actor(LEADER_ID, "Alice"))
    panel = channels[CHANNEL_ID].messages[meeting.panel_message_id]
    clicks = [FakeInteraction(bot, LEADER, channels[CHANNEL_ID], panel) for _ in range(2)]
    await asyncio.gather(*(cog.handle_button(c, meeting.id, "next", detail.meeting.revision) for c in clicks))
    assert [s.status for s in bot.meetings.get(meeting.id).steps] == ["done", "current", "pending"]
    refused = [c for c in clicks if c.followup.sent]
    assert len(refused) == 1 and "Panneau périmé" in refused[0].followup.sent[0]["embed"].title
    assert refused[0].response.edited  # le panneau périmé a été redessiné


async def test_deleted_panel_is_republished_once(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    channels[CHANNEL_ID].messages.clear()  # quelqu'un supprime le panneau
    interaction = FakeInteraction(bot, MEMBER, channels[CHANNEL_ID])
    await cog.point.callback(cog, interaction, texte="Questions diverses", reunion=meeting.id)
    new_id = bot.meetings.repo.get(meeting.id).panel_message_id
    assert new_id != meeting.panel_message_id and list(channels[CHANNEL_ID].messages) == [new_id]
    # Les actualisations suivantes modifient ce nouveau panneau au lieu d'en créer d'autres.
    await cog.rejoindre.callback(cog, FakeInteraction(bot, OUTSIDER, channels[CHANNEL_ID]), reunion=None)
    assert list(channels[CHANNEL_ID].messages) == [new_id]
    assert channels[CHANNEL_ID].edits[-1][0] == new_id


async def test_full_meeting_from_discord_commands(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    room = channels[CHANNEL_ID]

    async def run(command, user=LEADER, **kwargs):
        interaction = FakeInteraction(bot, user, room)
        await command.callback(cog, interaction, **kwargs)
        return interaction

    await run(cog.demarrer)
    await run(cog.decision, texte="On garde Pygame", motif="déjà maîtrisé")
    await run(cog.reserve, user=MEMBER, texte="Planning serré")
    await run(cog.action, user=MEMBER, description="Écrire le README", responsable=MEMBER, echeance="05/11/2026")
    await run(cog.suivante)
    refused = await run(cog.decision, user=MEMBER, texte="Je décide seul")
    assert "Accès refusé" in refused.response.sent[0]["embed"].title
    progress = await run(cog.progression)
    assert "1/3 étapes traitées" in progress.response.sent[0]["embed"].description

    ask = await run(cog.terminer)
    assert "Terminer la réunion" in ask.response.sent[0]["content"]
    view = ask.response.sent[0]["view"]
    confirm = FakeInteraction(bot, LEADER, room)
    await view.confirm.callback(confirm)
    detail = bot.meetings.get(meeting.id)
    assert detail.meeting.status == FINISHED
    assert "Compte rendu publié" in confirm.edits[-1]["embed"].description

    report_channel = channels[DECISIONS_CHANNEL_ID]
    assert list(report_channel.messages) == detail.meeting.report_messages
    report = " ".join(text_of(m.embed) for m in report_channel.messages.values())
    for expected in ("Participants", "Objectifs", "Étapes traitées (2/3)", "On garde Pygame", "Planning serré", "Écrire le README", "2026-11-05"):
        assert expected in report, expected
    # Le panneau d'origine est mis à jour (terminé, lien vers le compte rendu, plus de bouton de contrôle).
    panel = room.messages[detail.meeting.panel_message_id]
    assert "Terminée" in text_of(panel.embed) and "Lire le compte rendu" in text_of(panel.embed)

    # Republier un compte rendu toujours présent ne crée pas de doublon.
    again = await run(cog.compte_rendu, reunion=meeting.id, publier=True)
    assert len(report_channel.messages) == len(detail.meeting.report_messages)
    assert "Compte rendu publié" in again.followup.sent[0]["embed"].title


async def test_non_leader_cannot_open_the_close_confirmation(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    bot.meetings.start(meeting.id, Actor(LEADER_ID, "Alice"))
    click = FakeInteraction(bot, MEMBER, channels[CHANNEL_ID])
    await cog.handle_button(click, meeting.id, "finish", 0)
    assert "Accès refusé" in click.response.sent[0]["embed"].title
    assert bot.meetings.get(meeting.id).meeting.status == RUNNING


async def test_admin_can_control_any_meeting(env, monkeypatch):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    monkeypatch.setattr(cog, "_actor", lambda interaction: Actor(interaction.user.id, "Admin", is_admin=True))
    interaction = FakeInteraction(bot, OUTSIDER, channels[CHANNEL_ID])
    await cog.demarrer.callback(cog, interaction, reunion=meeting.id)
    assert bot.meetings.get(meeting.id).meeting.status == RUNNING


async def test_report_preview_is_paginated(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    actor = Actor(LEADER_ID, "Alice")
    bot.meetings.start(meeting.id, actor)
    for index in range(40):
        with bot.meetings.repo.change(meeting.id, actor.ref, "test") as change:
            change.add_note(DECISION if index % 2 else RESERVE, f"Note {index} " + "x" * 400, None, 1, LEADER_ID, "Alice")
    interaction = FakeInteraction(bot, MEMBER, channels[CHANNEL_ID])
    await cog.compte_rendu.callback(cog, interaction, reunion=meeting.id, publier=False)
    sent = interaction.response.sent[0]
    assert sent["ephemeral"] and "aperçu provisoire" in sent["embed"].footer.text
    view = sent["view"]
    assert len(view.pages) > 1
    for page in view.pages:
        assert len(page) <= limits.EMBED_TOTAL and len(page.description) <= limits.EMBED_DESCRIPTION
    assert "page 1/" in view.pages[0].footer.text
    # Le panneau reste dans les limites de Discord malgré les 40 notes.
    detail = bot.meetings.get(meeting.id)
    embed = panel_embed(detail)
    assert len(embed) <= limits.EMBED_TOTAL and "et 16 de plus" in text_of(embed)


async def test_buttons_survive_a_restart(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    custom_id = button_ids(panel_view(bot.meetings.get(meeting.id)))[3]
    # Après redémarrage, discord.py reconstruit le bouton à partir de son seul identifiant.
    match = re.fullmatch(BUTTON_TEMPLATE, custom_id)
    button = await MeetingButton.from_custom_id(None, None, match)
    assert (button.meeting_id, button.action, button.revision) == (meeting.id, "start", meeting.revision)
    assert len(custom_id) <= 100
    with pytest.raises(ValueError):
        await MeetingButton.from_custom_id(None, None, re.fullmatch(BUTTON_TEMPLATE, f"samae:mtg:{meeting.id}:drop:0"))
    click = FakeInteraction(bot, LEADER, channels[CHANNEL_ID])
    await button.callback(click)
    assert bot.meetings.get(meeting.id).meeting.status == RUNNING


async def test_default_meeting_and_unknown_meeting(env):
    bot, cog, channels = env
    interaction = FakeInteraction(bot, MEMBER, channels[CHANNEL_ID])
    await cog.voir.callback(cog, interaction, reunion=None, publier=False)
    assert "Aucune réunion" in interaction.response.sent[0]["embed"].description
    interaction = FakeInteraction(bot, MEMBER, channels[CHANNEL_ID])
    await cog.demarrer.callback(cog, interaction, reunion=999)
    assert "n'existe pas" in interaction.response.sent[0]["embed"].description


async def test_move_panel_replaces_the_old_one(env):
    bot, cog, channels = env
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    other = FakeChannel(300)
    channels[300] = other
    interaction = FakeInteraction(bot, LEADER, other)
    await cog.voir.callback(cog, interaction, reunion=meeting.id, publier=True)
    moved = bot.meetings.repo.get(meeting.id)
    assert moved.channel_id == 300 and list(other.messages) == [moved.panel_message_id]
    assert channels[CHANNEL_ID].messages == {}  # ancien panneau retiré après publication du nouveau
    refused = FakeInteraction(bot, MEMBER, other)
    await cog.voir.callback(cog, refused, reunion=meeting.id, publier=True)
    assert "Accès refusé" in refused.response.sent[0]["embed"].title


def test_report_embeds_keep_mentions_intact(db):
    from src.database.meetings import MeetingRepository
    from src.services.meetings import MeetingService

    service = MeetingService(MeetingRepository(db))
    m = service.create(Actor(LEADER_ID, "Alice"), title="Revue").meeting
    pages = report_embeds(service.report(m.id))
    assert f"<@{LEADER_ID}>" in pages[0].description  # pas d'échappement qui casserait la mention


async def test_reunion_help_is_registered_and_paginated(env):
    bot, cog, channels = env
    interaction = FakeInteraction(bot, MEMBER, channels[CHANNEL_ID])
    await cog.aide.callback(cog, interaction)
    sent = interaction.response.sent[0]
    assert sent["ephemeral"]
    assert sent["embed"].title.startswith("Aide des réunions · Créer une réunion")
    assert "/reunion creer titre:" in text_of(sent["embed"])
    assert all(len(page.embed) <= limits.EMBED_TOTAL for page in sent["view"].pages)


async def test_report_prefers_the_minutes_channel_after_redesign(env):
    bot, cog, channels = env
    channels[300] = FakeChannel(300)
    bot.bindings_repo.replace_all({"decisions": DECISIONS_CHANNEL_ID, "comptes_rendus": 300})
    await create(bot, cog, channels)
    meeting = bot.meetings.repo.list()[0]
    leader = Actor(LEADER_ID, "Alice")
    bot.meetings.start(meeting.id, leader)
    bot.meetings.finish(meeting.id, leader)
    url, problem = await cog.publish_report(bot.meetings.get(meeting.id))
    assert problem is None and "/300/" in url
    assert channels[300].messages and not channels[DECISIONS_CHANNEL_ID].messages
