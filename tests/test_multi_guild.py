"""Multi-serveurs (1.12) : migration v14, réglages par serveur, isolation des réunions, serveur principal, /setup.

Aucun token, aucun Discord réel : bases SQLite temporaires et faux objets Discord.
"""

from __future__ import annotations

import random
import sqlite3
from types import SimpleNamespace

import pytest
from discord.ext import commands

from src.config import Settings
from src.database import Database
from src.database.connection import MIGRATIONS
from src.database.guild_settings import GuildSettingsRepository
from src.database.meetings import MeetingRepository
from src.discord_bot.bot import EXTENSIONS, HOME_GUILD_KEY, SaeBot, TeamSpaceOnly, require_team_space
from src.music.models import QueueEntry
from src.music.queue import TrackQueue
from src.music.search import normalize_track
from src.services.meetings import Actor, MeetingError, MeetingService
from tests.music_fakes import raw_track

HOME, OTHER = 42, 43
V14 = 14


# --- Migration v14 --------------------------------------------------------------------------------


def build_v13(path) -> None:
    conn = sqlite3.connect(path)
    for number, script in enumerate(MIGRATIONS[:13], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute("INSERT INTO meetings (title, leader_id, leader_name, created_by) VALUES ('Ancienne réunion', 1, 'Alice', 1)")
    conn.execute("INSERT INTO music_guild_settings (guild_id, volume) VALUES (42, 30)")
    conn.commit()
    conn.close()


def test_v14_only_adds_a_table_a_column_and_an_index():
    script = MIGRATIONS[V14 - 1].upper()
    assert "CREATE TABLE GUILD_SETTINGS" in script and "ADD COLUMN GUILD_ID" in script
    assert "DROP " not in script and "DELETE " not in script and "UPDATE " not in script


def test_upgrade_from_v13_keeps_meetings_and_music_settings(tmp_path):
    path = tmp_path / "v13.sqlite3"
    build_v13(path)
    db = Database(path, backup_dir=tmp_path / "backups")
    try:
        assert db.schema_version == len(MIGRATIONS) >= V14
        row = db.conn.execute("SELECT title, guild_id FROM meetings").fetchone()
        assert row["title"] == "Ancienne réunion" and row["guild_id"] is None  # rattachée plus tard au serveur principal
        assert db.conn.execute("SELECT volume FROM music_guild_settings WHERE guild_id = 42").fetchone()[0] == 30
        assert list((tmp_path / "backups").glob("*avantv*"))  # sauvegarde vérifiée avant la migration
        assert db.integrity_ok()
        assert MeetingRepository(db).adopt_orphans(HOME) == 1
        assert MeetingRepository(db).adopt_orphans(HOME) == 0  # une seule fois
        assert db.conn.execute("SELECT guild_id FROM meetings").fetchone()[0] == HOME
    finally:
        db.close()


# --- Réglages par serveur ----------------------------------------------------------------------


def test_guild_settings_are_per_guild(db):
    repo = GuildSettingsRepository(db)
    assert repo.get(HOME).music_dj_role_id is None
    repo.set_dj_role(HOME, 555)
    repo.set_dj_role(OTHER, 666)
    repo.set_dj_role(HOME, 777)  # remplacement, sans doublon
    assert repo.get(HOME).music_dj_role_id == 777 and repo.get(OTHER).music_dj_role_id == 666
    repo.set_dj_role(HOME, None)
    assert repo.get(HOME).music_dj_role_id is None and repo.get(OTHER).music_dj_role_id == 666
    repo.forget(OTHER)
    assert repo.get(OTHER).music_dj_role_id is None
    assert db.conn.execute("SELECT COUNT(*) FROM guild_settings").fetchone()[0] == 1


# --- Réunions isolées par serveur ---------------------------------------------------------------


def test_meetings_of_another_guild_are_invisible(db):
    service = MeetingService(MeetingRepository(db))
    alice_home, alice_other = Actor(1, "Alice", guild_id=HOME), Actor(1, "Alice", guild_id=OTHER)
    mine = service.create(alice_home, title="Chez nous", objectives="", participants=[], steps=["Point"], planned_minutes=30,
                          channel_id=100)
    theirs = service.create(alice_other, title="Ailleurs", objectives="", participants=[], steps=["Point"], planned_minutes=30,
                            channel_id=200)
    assert mine.meeting.guild_id == HOME and theirs.meeting.guild_id == OTHER
    assert [m.title for m in MeetingRepository(db).list(guild_id=HOME)] == ["Chez nous"]
    with pytest.raises(MeetingError, match="n'existe pas"):
        service.get(theirs.meeting.id, HOME)
    with pytest.raises(MeetingError, match="n'existe pas"):
        service.start(theirs.meeting.id, alice_home)  # même personne, autre serveur : refus
    assert service.resolve(None, alice_home, 100).meeting.id == mine.meeting.id
    assert service.resolve(None, alice_home, 200).meeting.id == mine.meeting.id  # jamais celle de l'autre serveur
    assert service.resolve(None, alice_other, 100).meeting.id == theirs.meeting.id
    with pytest.raises(MeetingError, match="n'existe pas"):
        service.report(theirs.meeting.id, HOME)


# --- File d'attente : nouvelles opérations -------------------------------------------------------


def entry(title: str) -> QueueEntry:
    return QueueEntry(normalize_track(raw_track(title)), 1, "Alice")


def titles(queue: TrackQueue) -> list[str]:
    return [e.track.title for e in queue]


def test_queue_remove_peek_shuffle_and_room():
    queue = TrackQueue(3)
    for title in "abc":
        queue.add(entry(title))
    assert queue.peek_at(2).track.title == "b" and queue.peek_at(0) is None and queue.peek_at(4) is None
    assert queue.remove_at(2).track.title == "b" and titles(queue) == ["a", "c"]
    assert queue.remove_at(5) is None
    assert queue.push_front(entry("z")) and titles(queue) == ["z", "a", "c"]
    assert not queue.push_front(entry("plein")) and not queue.append_if_room(entry("plein"))  # limite respectée
    assert queue.shuffle(random.Random(1)) == 3 and sorted(titles(queue)) == ["a", "c", "z"]


# --- Serveur principal et espace d'équipe -------------------------------------------------------


async def make_bot(db, **env):
    bot = SaeBot(Settings.from_env(env, require_token=False), db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    return bot


def interaction_for(bot, guild_id):
    return SimpleNamespace(client=bot, guild_id=guild_id)


async def test_no_home_guild_means_no_team_space_anywhere(db):
    bot = await make_bot(db)
    try:
        assert bot.home_guild_id is None and bot.home_guild_source == "aucun"
        with pytest.raises(TeamSpaceOnly, match="serveur-principal"):
            require_team_space(interaction_for(bot, HOME))
    finally:
        await bot.close()


async def test_home_guild_from_env_wins_and_other_guilds_are_refused(db):
    bot = await make_bot(db, DISCORD_GUILD_ID=str(HOME))
    try:
        assert bot.home_guild_id == HOME and "DISCORD_GUILD_ID" in bot.home_guild_source
        assert require_team_space(interaction_for(bot, HOME)) is True
        with pytest.raises(TeamSpaceOnly, match="serveur principal"):
            require_team_space(interaction_for(bot, OTHER))
        bot.set_home_guild(OTHER)  # ignoré tant que DISCORD_GUILD_ID est rempli
        assert bot.home_guild_id == HOME
    finally:
        await bot.close()


async def test_home_guild_chosen_from_discord_adopts_old_meetings(db):
    db.conn.execute("INSERT INTO meetings (title, leader_id, leader_name, created_by) VALUES ('Avant 1.12', 1, 'Alice', 1)")
    db.conn.commit()
    bot = await make_bot(db)
    try:
        bot.set_home_guild(HOME)
        assert bot.home_guild_id == HOME and bot.state_repo.get(HOME_GUILD_KEY) == str(HOME)
        assert db.conn.execute("SELECT guild_id FROM meetings").fetchone()[0] == HOME
        assert require_team_space(interaction_for(bot, HOME)) is True
    finally:
        await bot.close()


async def test_team_space_installed_elsewhere_is_a_conflict(db):
    bot = await make_bot(db)
    try:
        bot.set_home_guild(HOME)
        bot.mark_setup_confirmed()  # /admin installer fait sur HOME
        assert bot.team_space_conflict is None
        bot.set_home_guild(OTHER)  # le propriétaire choisit un autre serveur
        assert bot.team_space_conflict == HOME
        with pytest.raises(TeamSpaceOnly, match="reinitialiser"):
            require_team_space(interaction_for(bot, OTHER))
        bot.reset_team_space()
        assert bot.team_space_conflict is None and require_team_space(interaction_for(bot, OTHER))
    finally:
        await bot.close()


@pytest.mark.parametrize("cog_name", ["AdminCog", "RedesignCog", "AICog", "ActivityCog", "WelcomeCog"])
async def test_team_space_cogs_are_guarded(db, cog_name):
    bot = await make_bot(db, DISCORD_GUILD_ID=str(HOME))
    try:
        cog = bot.get_cog(cog_name)
        assert await cog.interaction_check(interaction_for(bot, HOME)) is True
        with pytest.raises(TeamSpaceOnly):
            await cog.interaction_check(interaction_for(bot, OTHER))
    finally:
        await bot.close()


@pytest.mark.parametrize("cog_name", ["MusicCog", "MeetingsCog", "ProjectsCog", "GeneralCog", "SetupCog"])
async def test_general_cogs_work_on_any_guild(db, cog_name):
    bot = await make_bot(db, DISCORD_GUILD_ID=str(HOME))
    try:
        cog = bot.get_cog(cog_name)
        assert cog is not None
        # Pas de garde « espace d'équipe » : utilisables sur tous les serveurs.
        assert type(cog).interaction_check is commands.Cog.interaction_check
    finally:
        await bot.close()


# --- /setup --------------------------------------------------------------------------------------


class Response:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def is_done(self) -> bool:
        return bool(self.sent)

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self.sent.append(kwargs)


class Followup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class Role:
    def __init__(self, role_id, name="DJ", *, default=False, managed=False, guild=None) -> None:
        self.id, self.name, self.managed, self._default, self.guild = role_id, name, managed, default, guild
        self.mention = f"<@&{role_id}>"

    def is_default(self) -> bool:
        return self._default


class Guild:
    def __init__(self, guild_id: int) -> None:
        self.id, self.name = guild_id, f"Serveur {guild_id}"
        self.me = SimpleNamespace(voice=None)
        self.voice_channels, self.text_channels = [], []
        self.roles: dict[int, Role] = {}

    def get_role(self, role_id):
        return self.roles.get(role_id)


class Interaction:
    def __init__(self, bot, guild, user_id=1) -> None:
        self.client, self.guild, self.guild_id = bot, guild, guild.id
        self.user = SimpleNamespace(id=user_id, __str__=lambda self: "Alice")
        self.response, self.followup = Response(), Followup()
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


def text_of(interaction) -> str:
    parts = []
    for item in [*interaction.response.sent, *interaction.followup.sent, *interaction.edits]:
        if item.get("content"):
            parts.append(item["content"])
        embed = item.get("embed")
        if embed is not None:
            parts.append(" ".join([embed.title or "", embed.description or "", *[f"{f.name} {f.value}" for f in embed.fields]]))
    return "\n".join(parts)


async def test_setup_status_works_on_any_guild_without_secrets(db):
    bot = await make_bot(db, DISCORD_BOT_TOKEN="faux-token-secret")
    try:
        cog = bot.get_cog("SetupCog")
        interaction = Interaction(bot, Guild(OTHER))
        await cog.status.callback(cog, interaction)
        text = text_of(interaction)
        assert "Musique" in text and "Espace d'équipe" in text and "serveur-principal" in text
        assert "faux-token-secret" not in text
        assert interaction.response.sent[0]["ephemeral"]
    finally:
        await bot.close()


async def test_setup_dj_role_is_saved_for_this_guild_only(db):
    bot = await make_bot(db)
    try:
        cog = bot.get_cog("SetupCog")
        guild = Guild(OTHER)
        role = Role(555, guild=guild)
        interaction = Interaction(bot, guild)
        await cog.dj_role.callback(cog, interaction, role)
        assert "Rôle DJ de ce serveur" in text_of(interaction)
        assert bot.guild_settings_repo.get(OTHER).music_dj_role_id == 555
        assert bot.guild_settings_repo.get(HOME).music_dj_role_id is None
        for bad in (Role(OTHER, "@everyone", default=True, guild=guild), Role(9, "Bot", managed=True, guild=guild)):
            refused = Interaction(bot, guild)
            await cog.dj_role.callback(cog, refused, bad)
            assert "rôle normal" in text_of(refused)
        assert bot.guild_settings_repo.get(OTHER).music_dj_role_id == 555
        cleared = Interaction(bot, guild)
        await cog.dj_role.callback(cog, cleared, None)
        assert bot.guild_settings_repo.get(OTHER).music_dj_role_id is None
    finally:
        await bot.close()


async def test_setup_home_is_reserved_to_the_bot_owner(db, monkeypatch):
    bot = await make_bot(db)
    try:
        cog = bot.get_cog("SetupCog")

        async def not_owner(user):
            return False

        monkeypatch.setattr(bot, "is_owner", not_owner)
        interaction = Interaction(bot, Guild(OTHER))
        await cog.home.callback(cog, interaction, False)
        assert "propriétaire du bot" in text_of(interaction) and bot.home_guild_id is None
    finally:
        await bot.close()


async def test_setup_home_confirms_then_syncs(db, monkeypatch):
    bot = await make_bot(db)
    try:
        cog = bot.get_cog("SetupCog")

        async def owner(user):
            return True

        synced = []

        async def fake_sync(*, force=False):
            synced.append(bot.home_guild_id)
            return 5

        monkeypatch.setattr(bot, "is_owner", owner)
        monkeypatch.setattr(bot, "sync_commands", fake_sync)
        interaction = Interaction(bot, Guild(OTHER))
        await cog.home.callback(cog, interaction, False)
        view = interaction.response.sent[0]["view"]
        assert bot.home_guild_id is None  # rien avant la confirmation
        confirm = Interaction(bot, Guild(OTHER))
        await view._on_confirm(confirm)
        assert bot.home_guild_id == OTHER and synced == [OTHER]
        assert "serveur principal" in text_of(confirm)
        again = Interaction(bot, Guild(OTHER))
        await cog.home.callback(cog, again, False)
        assert "déjà le serveur principal" in text_of(again)
    finally:
        await bot.close()


async def test_setup_home_refused_when_env_names_another_guild(db, monkeypatch):
    bot = await make_bot(db, DISCORD_GUILD_ID=str(HOME))
    try:
        cog = bot.get_cog("SetupCog")

        async def owner(user):
            return True

        monkeypatch.setattr(bot, "is_owner", owner)
        interaction = Interaction(bot, Guild(OTHER))
        await cog.home.callback(cog, interaction, False)
        assert "DISCORD_GUILD_ID" in text_of(interaction) and bot.home_guild_id == HOME
    finally:
        await bot.close()


async def test_setup_commands_are_admin_only(db):
    bot = await make_bot(db)
    try:
        group = bot.tree.get_command("setup")
        assert group.default_permissions.manage_guild and group.guild_only
        assert {c.name for c in group.commands} == {"etat", "role-dj", "serveur-principal"}
        assert all(c.checks for c in group.commands)
    finally:
        await bot.close()
