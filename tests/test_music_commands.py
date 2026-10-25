"""Commandes de musique (/play, /skip, /loop…), boutons du panneau et événements, avec un faux Discord et un faux moteur audio."""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import discord
import pytest

from src.config import Settings
from src.database.music import MusicRepository
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.discord_bot import music_ui
from src.discord_bot.music_ui import BUTTON_TEMPLATE, MusicButton, player_embed, player_view
from src.music.lavalink import ENTRY_KEY, entry_id_of
from src.music.service import LoadResult, MusicConfig, MusicService
from src.services.health import MusicSummary, _music_check, HealthStatus
from tests.music_fakes import FakeBackend, raw_track

GUILD_ID, OTHER_GUILD = 42, 43
TEXT_ID, VOICE_ID, VOICE2_ID = 100, 200, 201
ALICE, BOB, CHLOE = 111111111111111111, 222222222222222222, 333333333333333333
_ids = itertools.count(9000)


class _Response:
    status = 404
    reason = "Not Found"


class FakeMessage:
    def __init__(self, channel, embed=None, view=None) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.embed = embed
        self.view = view
        self.edited: list[dict] = []

    async def edit(self, **kwargs):
        self.edited.append(kwargs)


class FakePartial:
    def __init__(self, channel, message_id) -> None:
        self.channel, self.id = channel, message_id

    async def edit(self, **kwargs):
        if self.id not in self.channel.messages:
            raise discord.NotFound(_Response(), "Unknown Message")
        message = self.channel.messages[self.id]
        message.embed, message.view = kwargs.get("embed"), kwargs.get("view")
        self.channel.edits += 1


class FakeTextChannel:
    def __init__(self, channel_id: int, guild) -> None:
        self.id = channel_id
        self.guild = guild
        self.messages: dict[int, FakeMessage] = {}
        self.edits = 0

    async def send(self, embed=None, view=None, **kwargs):
        assert kwargs.get("allowed_mentions") is not None, "aucun message public sans protection des mentions"
        message = FakeMessage(self, embed, view)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)


class Perms:
    def __init__(self, **overrides) -> None:
        self.view_channel = self.connect = self.speak = True
        self.move_members = False
        self.administrator = self.manage_guild = False
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeVoiceChannel:
    def __init__(self, channel_id: int, *, perms: Perms | None = None, user_limit: int = 0) -> None:
        self.id = channel_id
        self.members: list = []
        self.user_limit = user_limit
        self._perms = perms or Perms()

    def permissions_for(self, member):
        return self._perms


class FakeMember:
    def __init__(self, user_id: int, name: str, *, admin: bool = False, roles=(), bot: bool = False) -> None:
        self.id = user_id
        self.display_name = name
        self.bot = bot
        self.roles = [SimpleNamespace(name=r) for r in roles]
        self.guild_permissions = Perms(manage_guild=admin)
        self.voice = None

    def __str__(self) -> str:
        return self.display_name


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.channels: dict[int, object] = {}
        self.me = FakeMember(1, "Samae", bot=True)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deferred: list[dict] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs):
        assert not self._done, "une seule réponse par interaction"
        self.sent.append({"content": content, **kwargs})
        self._done = True

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)
        self._done = True

    async def defer(self, **kwargs):
        assert not self._done
        self.deferred.append(kwargs)
        self._done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return FakeMessage(None)


class FakeInteraction:
    def __init__(self, guild: FakeGuild, user: FakeMember, *, message=None) -> None:
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.channel_id = TEXT_ID
        self.message = message
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = None
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


def all_embeds(interaction: FakeInteraction) -> str:
    texts = []
    for item in [*interaction.response.sent, *interaction.followup.sent, *interaction.edits, *interaction.response.edited]:
        embed = item.get("embed")
        if embed is not None:
            texts.append(" ".join([embed.title or "", embed.description or "", *[f"{f.name} {f.value}" for f in embed.fields]]))
        if item.get("content"):
            texts.append(item["content"])
    return "\n".join(texts)


def join(member: FakeMember, channel: FakeVoiceChannel | None) -> None:
    for voice in (VOICE, VOICE2):
        if member in voice.members:
            voice.members.remove(member)
    member.voice = SimpleNamespace(channel=channel) if channel is not None else None
    if channel is not None:
        channel.members.append(member)


GUILD = FakeGuild(GUILD_ID)
VOICE = FakeVoiceChannel(VOICE_ID)
VOICE2 = FakeVoiceChannel(VOICE2_ID)


@pytest.fixture
async def env(db):
    settings = Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID), "MUSIC_DJ_ROLE_NAME": ""}, require_token=False)
    bot = SaeBot(settings, db)
    backend = FakeBackend()
    bot.music = MusicService(backend, MusicRepository(db), MusicConfig(sources=("youtubemusic", "soundcloud")))
    bot.music._clock = itertools.count(0, 10).__next__
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    GUILD.channels = {VOICE_ID: VOICE, VOICE2_ID: VOICE2}
    VOICE.members, VOICE2.members = [], []
    text = FakeTextChannel(TEXT_ID, GUILD)
    bot.get_channel = {TEXT_ID: text}.get  # type: ignore[method-assign]
    members = {name: FakeMember(uid, name) for name, uid in (("Alice", ALICE), ("Bob", BOB), ("Chloé", CHLOE))}
    cog = bot.get_cog("MusicCog")
    cog.panel_delay = 0
    cog.audio_check_delay = 0  # pas de vérification du son programmée (testée à part)
    yield SimpleNamespace(bot=bot, cog=cog, backend=backend, text=text, m=members)
    await bot.close()


async def flush(cog) -> None:
    """Attend la fin des actualisations du panneau programmées."""
    for task in list(cog._panel_tasks.values()):
        await task


async def run_play(env, member, query, source=None):
    interaction = FakeInteraction(GUILD, member)
    await env.cog.play.callback(env.cog, interaction, query, source)
    await flush(env.cog)
    return interaction


def panel(env) -> FakeMessage:
    """Panneau le plus récent (une nouvelle session publie son propre panneau)."""
    assert env.text.messages
    return env.text.messages[max(env.text.messages)]


def ids(view) -> list[str]:
    return [item.item.custom_id for item in view.children]


# --- Désactivée ou indisponible ---------------------------------------------------------------


async def test_disabled_music_answers_clearly(db):
    bot = SaeBot(Settings.from_env({"DISCORD_GUILD_ID": "42"}, require_token=False), db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    try:
        cog = bot.get_cog("MusicCog")
        interaction = FakeInteraction(GUILD, FakeMember(ALICE, "Alice"))
        await cog.play.callback(cog, interaction, "titre", None)
        assert "MUSIC_ENABLED=true" in all_embeds(interaction) and interaction.response.sent[0]["ephemeral"]
        interaction = FakeInteraction(GUILD, FakeMember(ALICE, "Alice"))
        await cog.queue.callback(cog, interaction)
        assert "pas activée" in all_embeds(interaction)
    finally:
        await bot.close()


async def test_lavalink_down_does_not_break_other_commands(env):
    env.backend.ready = False
    join(env.m["Alice"], VOICE)
    interaction = await run_play(env, env.m["Alice"], "titre")
    assert "Lavalink" in all_embeds(interaction) and env.backend.calls == []
    assert env.bot.get_cog("MeetingsCog") is not None  # le reste du bot est chargé normalement


# --- Lecture -------------------------------------------------------------------------------------


async def test_play_link_connects_plays_and_publishes_one_panel(env):
    url = "https://www.youtube.com/watch?v=abc"
    env.backend.results[url] = LoadResult([raw_track("Un titre", identifier="abc")])
    join(env.m["Alice"], VOICE)
    interaction = await run_play(env, env.m["Alice"], url)
    assert interaction.response.deferred[0] == {"ephemeral": True, "thinking": True}  # réponse en moins de 3 s
    assert "lancé maintenant" in all_embeds(interaction)
    assert env.backend.connected == {GUILD_ID: VOICE_ID}
    message = panel(env)
    assert "Un titre" in message.embed.title and f"<@{ALICE}>" in " ".join(f.value for f in message.embed.fields)
    assert [cid.split(":")[-1] for cid in ids(message.view)] == ["pause", "skip", "stop", "loop", "queue", "favorite"]
    assert all(cid.startswith(f"samae:music:{GUILD_ID}:") for cid in ids(message.view))


async def test_play_with_text_plays_the_first_result(env):
    env.backend.results["ytmsearch:daft punk"] = LoadResult([raw_track(f"Résultat {i}", identifier=f"r{i}") for i in range(7)])
    join(env.m["Alice"], VOICE)
    interaction = await run_play(env, env.m["Alice"], "daft punk")
    assert "lancé maintenant" in all_embeds(interaction) and "Résultat 0" in panel(env).embed.title
    assert env.backend.count("play") == 1 and env.bot.music.snapshot(GUILD_ID).upcoming == ()


async def run_search(env, member, query, source=None):
    interaction = FakeInteraction(GUILD, member)
    await env.cog.search.callback(env.cog, interaction, query, source)
    await flush(env.cog)
    return interaction


async def test_search_offers_a_choice_reserved_to_the_requester(env):
    env.backend.results["ytmsearch:daft punk"] = LoadResult([raw_track(f"Résultat {i}") for i in range(7)])
    join(env.m["Alice"], VOICE)
    interaction = await run_search(env, env.m["Alice"], "daft punk")
    assert interaction.response.deferred  # « réfléchit… » pendant la recherche
    sent = interaction.followup.sent[0]
    view = sent["view"]
    assert sent["ephemeral"] and len(view.select.options) == 5 and env.backend.count("play") == 0
    # Quelqu'un d'autre ne peut pas choisir à sa place.
    intruder = FakeInteraction(GUILD, env.m["Bob"])
    assert await view.interaction_check(intruder) is False
    # Alice choisit le 2e résultat.
    choice = FakeInteraction(GUILD, env.m["Alice"])
    view.select._values = ["1"]
    assert await view.interaction_check(choice)
    await view._picked(choice)
    await flush(env.cog)
    assert "Résultat 1" in panel(env).embed.title
    assert "lancé maintenant" in all_embeds(choice)
    assert await view.interaction_check(FakeInteraction(GUILD, env.m["Alice"])) is False  # second clic ignoré


async def test_search_requires_voice_before_deferring(env):
    interaction = await run_search(env, env.m["Alice"], "a")
    assert "Rejoignez d'abord un salon vocal" in all_embeds(interaction)
    assert not interaction.response.deferred and env.backend.count("load") == 0


async def test_second_play_queues_instead_of_interrupting(env):
    env.backend.results["ytmsearch:a"] = LoadResult([raw_track("A", identifier="a")])
    env.backend.results["https://soundcloud.com/x/b"] = LoadResult([raw_track("B", source="soundcloud", identifier="b")])
    join(env.m["Alice"], VOICE)
    join(env.m["Bob"], VOICE)
    await run_play(env, env.m["Alice"], "a")
    interaction = await run_play(env, env.m["Bob"], "https://soundcloud.com/x/b")
    assert "position 1" in all_embeds(interaction) and env.backend.count("play") == 1
    assert "Suivant" in " ".join(f.name + f.value for f in panel(env).embed.fields)


async def test_play_requires_voice_and_the_same_channel(env):
    env.backend.results["ytmsearch:a"] = LoadResult([raw_track("A")])
    interaction = await run_play(env, env.m["Alice"], "a")
    assert "Rejoignez d'abord un salon vocal" in all_embeds(interaction)
    join(env.m["Alice"], VOICE)
    await run_play(env, env.m["Alice"], "a")
    join(env.m["Bob"], VOICE2)
    interaction = await run_play(env, env.m["Bob"], "a")
    assert "rejoignez ce salon" in all_embeds(interaction)


async def test_bot_permissions_and_full_channels_are_checked(env):
    join(env.m["Alice"], VOICE)
    VOICE._perms = Perms(speak=False)
    try:
        interaction = await run_play(env, env.m["Alice"], "a")
        assert "Parler" in all_embeds(interaction) and env.backend.calls == []
        VOICE._perms = Perms()
        VOICE.user_limit = 1
        interaction = await run_play(env, env.m["Alice"], "a")
        assert "plein" in all_embeds(interaction)
    finally:
        VOICE._perms, VOICE.user_limit = Perms(), 0


async def test_internal_links_are_refused_before_lavalink(env):
    join(env.m["Alice"], VOICE)
    interaction = await run_play(env, env.m["Alice"], "http://127.0.0.1:2333/v4/sessions")
    assert "Recherche refusée" in all_embeds(interaction) and env.backend.count("load") == 0


# --- Boutons ---------------------------------------------------------------------------------


async def start_session(env, *titles):
    join(env.m["Alice"], VOICE)
    for title in titles:
        env.backend.results[f"ytmsearch:{title}"] = LoadResult([raw_track(title, identifier=title)])
        await run_play(env, env.m["Alice"], title)
    return panel(env)


def click_ids(message):
    return {cid.split(":")[-1]: cid for cid in ids(message.view)}


async def press(env, member, custom_id, *, guild=GUILD, message=None):
    match = __import__("re").fullmatch(BUTTON_TEMPLATE, custom_id)
    interaction = FakeInteraction(guild, member, message=message)
    await env.cog.handle_button(interaction, int(match["guild"]), match["session"], int(match["entry"]), match["action"])
    await flush(env.cog)
    return interaction


async def test_pause_button_toggles_and_redraws_the_panel(env):
    message = await start_session(env, "a")
    interaction = await press(env, env.m["Alice"], click_ids(message)["pause"])
    assert "mise en pause" in all_embeds(interaction)
    assert env.bot.music.snapshot(GUILD_ID).paused
    assert "resume" in click_ids(message) and "En pause" in message.embed.description
    interaction = await press(env, env.m["Alice"], click_ids(message)["resume"])
    assert "reprise" in all_embeds(interaction) and not env.bot.music.snapshot(GUILD_ID).paused


async def test_old_panel_buttons_never_control_a_new_session(env):
    message = await start_session(env, "a", "b")
    old = click_ids(message)
    await env.bot.music.leave(GUILD_ID, None)
    await flush(env.cog)
    assert message.view is None  # boutons retirés à la fin de la session
    new_panel = await start_session(env, "c")
    assert new_panel is not message and len(env.text.messages) == 2
    interaction = await press(env, env.m["Alice"], old["stop"], message=message)
    assert "session de musique terminée" in all_embeds(interaction)
    assert env.bot.music.snapshot(GUILD_ID).current.track.title == "c"


async def test_button_for_a_track_that_already_changed_is_refused(env):
    message = await start_session(env, "a", "b")
    old_skip = click_ids(message)["skip"]
    await env.bot.music.skip(GUILD_ID, env.cog._listener(FakeInteraction(GUILD, env.m["Alice"])))
    interaction = await press(env, env.m["Alice"], old_skip)
    assert "ne correspond plus" in all_embeds(interaction)
    assert env.bot.music.snapshot(GUILD_ID).current.track.title == "b"  # pas de second « suivant »


async def test_button_from_another_guild_is_refused(env):
    message = await start_session(env, "a")
    other = FakeGuild(OTHER_GUILD)
    interaction = await press(env, env.m["Bob"], click_ids(message)["stop"], guild=other)
    assert "n'appartient pas à ce serveur" in all_embeds(interaction)
    assert env.bot.music.snapshot(GUILD_ID).current is not None


async def test_button_permissions_are_checked_at_each_click(env):
    message = await start_session(env, "a", "b")
    # Bob n'est pas dans le salon vocal : refus, sans rien changer.
    interaction = await press(env, env.m["Bob"], click_ids(message)["skip"])
    assert "Rejoignez" in all_embeds(interaction)
    # Sans rôle DJ, toute personne du salon peut passer le titre.
    join(env.m["Bob"], VOICE)
    # Rôle DJ choisi pour CE serveur (/setup role-dj) : Bob, sans le rôle, ne peut plus passer le titre d'Alice.
    env.bot.guild_settings_repo.set_dj_role(GUILD_ID, 777)
    interaction = await press(env, env.m["Bob"], click_ids(message)["skip"])
    assert "Seule la personne qui a demandé" in all_embeds(interaction)
    env.m["Bob"].roles = [SimpleNamespace(name="autre nom", id=777)]  # reconnu par son identifiant, pas son nom
    interaction = await press(env, env.m["Bob"], click_ids(message)["skip"])
    assert "Passé" in all_embeds(interaction)
    env.m["Bob"].roles = []


async def test_queue_and_favorite_buttons(env, db):
    message = await start_session(env, "a", "b")
    interaction = await press(env, env.m["Bob"], click_ids(message)["queue"])
    assert "File d'attente" in all_embeds(interaction) and "b" in all_embeds(interaction)
    interaction = await press(env, env.m["Bob"], click_ids(message)["favorite"])
    assert "ajouté à vos favoris" in all_embeds(interaction)
    assert [f.title for f in MusicRepository(db).favorites(GUILD_ID, BOB)] == ["a"]
    interaction = await press(env, env.m["Bob"], click_ids(message)["favorite"])
    assert "retiré de vos favoris" in all_embeds(interaction)


async def test_deleted_panel_is_republished_once(env):
    await start_session(env, "a", "b")
    env.text.messages.clear()  # supprimé à la main
    current = env.bot.music.snapshot(GUILD_ID).current
    await env.bot.music.on_track_end(GUILD_ID, current.entry_id, "finished")
    await flush(env.cog)
    assert len(env.text.messages) == 1 and "b" in panel(env).embed.title  # un seul panneau, republié


async def test_dynamic_button_parsing(env):
    button = MusicButton(GUILD_ID, "0123abcd", 77, "skip")
    match = __import__("re").fullmatch(BUTTON_TEMPLATE, button.item.custom_id)
    parsed = await MusicButton.from_custom_id(None, button.item, match)
    assert (parsed.guild_id, parsed.session_id, parsed.entry_id, parsed.action) == (GUILD_ID, "0123abcd", 77, "skip")
    assert len(button.item.custom_id) <= 100
    bad = __import__("re").fullmatch(BUTTON_TEMPLATE, f"samae:music:{GUILD_ID}:0123abcd:1:explode")
    with pytest.raises(ValueError):
        await MusicButton.from_custom_id(None, button.item, bad)


# --- Commandes de contrôle et de consultation ---------------------------------------------------


async def test_control_commands(env):
    await start_session(env, "a", "b", "c")
    alice = env.m["Alice"]

    async def call(command, *args):
        interaction = FakeInteraction(GUILD, alice)
        await getattr(env.cog, command).callback(env.cog, interaction, *args)
        await flush(env.cog)
        return all_embeds(interaction)

    assert "mise en pause" in await call("pause")
    assert "déjà en pause" in await call("pause")
    assert "reprise" in await call("resume")
    assert "Passé" in await call("skip")
    assert "Volume réglé sur **35 %**" in await call("volume", 35)
    assert "35 %" in await call("volume", None)
    assert "🎵 b" in await call("now_playing")
    assert "File d'attente" in await call("queue")
    assert "ajouté à vos favoris" in await call("favorite_add")
    assert "déjà dans vos favoris" in await call("favorite_add")  # /favorite add n'enlève jamais
    assert "Vos favoris" in await call("favorite_list")
    assert "Retiré de vos favoris" in await call("favorite_remove", 1)
    assert "1 titre(s) retiré(s)" in await call("stop")
    assert "Historique musical" in await call("history")
    assert "quitté le salon vocal" in await call("leave")
    assert "Aucun titre n'est en cours" in await call("now_playing")
    assert "dans aucun salon vocal" in await call("pause")


async def test_history_is_limited_to_this_guild(env, db):
    from src.music.search import normalize_track

    MusicRepository(db).record_play(OTHER_GUILD, normalize_track(raw_track("secret d'un autre serveur")), BOB)
    interaction = FakeInteraction(GUILD, env.m["Alice"])
    await env.cog.history.callback(env.cog, interaction)
    assert "secret" not in all_embeds(interaction) and "Aucune lecture" in all_embeds(interaction)


async def test_favorites_replay_menu(env):
    await start_session(env, "a")
    service = env.bot.music
    service.toggle_favorite(GUILD_ID, ALICE)
    await service.stop(GUILD_ID, env.cog._listener(FakeInteraction(GUILD, env.m["Alice"])))
    uri = service.favorites(GUILD_ID, ALICE)[0].uri
    env.backend.results[uri] = LoadResult([raw_track("a", identifier="a")])
    interaction = FakeInteraction(GUILD, env.m["Alice"])
    await env.cog.favorite_list.callback(env.cog, interaction)
    view = interaction.followup.sent[-1]["view"]
    choice = FakeInteraction(GUILD, env.m["Alice"])
    view.select._values = ["0"]
    await view._picked(choice)
    await flush(env.cog)
    assert service.snapshot(GUILD_ID).current.track.title == "a" and "lancé maintenant" in all_embeds(choice)


# --- Événements ---------------------------------------------------------------------------------


def wavelink_payload(entry_id, reason="finished", guild_id=GUILD_ID):
    track = SimpleNamespace(extras={ENTRY_KEY: entry_id} if entry_id is not None else {})
    return SimpleNamespace(player=SimpleNamespace(guild=SimpleNamespace(id=guild_id)), track=track, reason=reason)


async def test_wavelink_track_end_event_advances_once(env):
    await start_session(env, "a", "b")
    current = env.bot.music.snapshot(GUILD_ID).current
    await env.cog.on_wavelink_track_end(wavelink_payload(current.entry_id))
    await env.cog.on_wavelink_track_end(wavelink_payload(current.entry_id))  # doublon : ignoré
    await flush(env.cog)
    assert env.bot.music.snapshot(GUILD_ID).current.track.title == "b"
    await env.cog.on_wavelink_track_end(SimpleNamespace(player=None, track=None, reason="finished"))  # lecteur inconnu


async def test_voice_state_and_node_events(env):
    await start_session(env, "a")
    bot_member = SimpleNamespace(id=4242, guild=SimpleNamespace(id=GUILD_ID))
    env.bot._connection.user = SimpleNamespace(id=4242)  # type: ignore[assignment]
    try:
        before, after = SimpleNamespace(channel=VOICE), SimpleNamespace(channel=None)
        await env.cog.on_voice_state_update(bot_member, before, after)
        assert not env.bot.music.snapshot(GUILD_ID).session_id
        await start_session(env, "b")
        assert await env.bot.music.reset_all("x") == 1
    finally:
        env.bot._connection.user = None


async def test_inactive_player_leaves(env):
    await start_session(env, "a")
    await env.cog.on_wavelink_inactive_player(SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID)))
    assert env.bot.music.snapshot(GUILD_ID).session_id == ""


def test_entry_id_is_read_from_track_extras():
    assert entry_id_of(SimpleNamespace(extras={ENTRY_KEY: 5})) == 5
    assert entry_id_of(SimpleNamespace(extras={ENTRY_KEY: "5"})) is None
    assert entry_id_of(SimpleNamespace(extras={ENTRY_KEY: True})) is None
    assert entry_id_of(None) is None


# --- Affichage et état ---------------------------------------------------------------------------


def test_panel_view_without_session_has_no_buttons(db):
    service = MusicService(FakeBackend(), MusicRepository(db), MusicConfig(sources=("soundcloud",)))
    snapshot = service.snapshot(GUILD_ID)
    assert player_view(snapshot) is None
    assert "arrêté" in player_embed(snapshot).description


def test_music_health_line():
    assert _music_check(MusicSummary()).status is HealthStatus.DISABLED
    assert _music_check(MusicSummary(config_error="LAVALINK_PASSWORD")).status is HealthStatus.WARNING
    assert _music_check(MusicSummary(enabled=True)).status is HealthStatus.ERROR
    ok = _music_check(MusicSummary(enabled=True, lavalink_ready=True, active_players=1, sources=("soundcloud",)))
    assert ok.status is HealthStatus.OK and "soundcloud" in ok.detail


def test_mentions_and_markdown_are_neutralised_in_titles():
    from src.music.search import normalize_track

    track = normalize_track(raw_track("**@everyone** `x`"))
    line = music_ui.track_line(track)
    assert "\\*\\*" in line and "\\`" in line


# --- Nouvelles commandes (1.12) : file, répétition, /join, diagnostic ------------------------------


async def call(env, member, command, *args, guild=GUILD):
    interaction = FakeInteraction(guild, member)
    await getattr(env.cog, command).callback(env.cog, interaction, *args)
    await flush(env.cog)
    return all_embeds(interaction)


def choice(value):
    return SimpleNamespace(value=value, name=value)


async def test_queue_management_commands(env):
    await start_session(env, "a", "b", "c", "d")
    alice = env.m["Alice"]
    assert [e.track.title for e in env.bot.music.snapshot(GUILD_ID).upcoming] == ["b", "c", "d"]
    assert "Retiré de la file" in await call(env, alice, "remove", 2)
    assert [e.track.title for e in env.bot.music.snapshot(GUILD_ID).upcoming] == ["b", "d"]
    assert "Aucun titre en position 9" in await call(env, alice, "remove", 9)
    assert "File mélangée (2 titres)" in await call(env, alice, "shuffle")
    assert "2 titre(s) retiré(s)" in await call(env, alice, "clear")
    assert env.bot.music.snapshot(GUILD_ID).current.track.title == "a"  # le titre en cours continue
    assert "déjà vide" in await call(env, alice, "clear")
    assert "au moins deux titres" in await call(env, alice, "shuffle")


async def test_remove_autocomplete_lists_this_guilds_queue(env):
    await start_session(env, "a", "b", "c")
    interaction = FakeInteraction(GUILD, env.m["Alice"])
    choices = await env.cog._remove_autocomplete(interaction, "")
    assert [(c.value, c.name.split(" ")[1]) for c in choices] == [(1, "b"), (2, "c")]
    other = FakeInteraction(FakeGuild(OTHER_GUILD), env.m["Alice"])
    assert await env.cog._remove_autocomplete(other, "") == []


async def test_removing_someone_elses_track_needs_dj(env):
    await start_session(env, "a")
    join(env.m["Bob"], VOICE)
    env.backend.results["ytmsearch:bob"] = LoadResult([raw_track("bob", identifier="bob")])
    await run_play(env, env.m["Bob"], "bob")
    env.bot.guild_settings_repo.set_dj_role(GUILD_ID, 777)  # rôle DJ sur ce serveur
    join(env.m["Chloé"], VOICE)
    refused = await call(env, env.m["Chloé"], "remove", 1)
    assert "Seule la personne" in refused or "DJ" in refused
    assert len(env.bot.music.snapshot(GUILD_ID).upcoming) == 1
    assert "Retiré de la file" in await call(env, env.m["Bob"], "remove", 1)  # son propre titre : toujours permis


async def test_loop_track_and_queue(env):
    await start_session(env, "a", "b")
    service, alice = env.bot.music, env.m["Alice"]
    assert "titre en cours" in await call(env, alice, "loop", choice("track"))
    current = service.snapshot(GUILD_ID).current
    await service.on_track_end(GUILD_ID, current.entry_id, "finished")
    assert service.snapshot(GUILD_ID).current.track.title == "a"  # le même titre repart
    assert "toute la file" in await call(env, alice, "loop", choice("queue"))
    assert "répétition : toute la file" in panel(env).embed.footer.text  # mode visible sur le panneau
    current = service.snapshot(GUILD_ID).current
    await service.on_track_end(GUILD_ID, current.entry_id, "finished")
    snap = service.snapshot(GUILD_ID)
    assert snap.current.track.title == "b" and [e.track.title for e in snap.upcoming] == ["a"]
    await call(env, alice, "skip")  # passer en mode file : le titre passé revient aussi à la fin
    snap = service.snapshot(GUILD_ID)
    assert snap.current.track.title == "a" and [e.track.title for e in snap.upcoming] == ["b"]
    assert "désactivée" in await call(env, alice, "loop", choice("off"))
    assert "répétition" not in panel(env).embed.footer.text


async def test_loop_button_cycles_the_mode(env):
    message = await start_session(env, "a")
    interaction = await press(env, env.m["Alice"], click_ids(message)["loop"])
    assert "titre en cours" in all_embeds(interaction)
    interaction = await press(env, env.m["Alice"], click_ids(message)["loop"])
    assert "toute la file" in all_embeds(interaction)


async def test_failed_track_is_never_looped(env):
    await start_session(env, "a", "b")
    service = env.bot.music
    await call(env, env.m["Alice"], "loop", choice("track"))
    current = service.snapshot(GUILD_ID).current
    await service.on_track_end(GUILD_ID, current.entry_id, "loadFailed")
    assert service.snapshot(GUILD_ID).current.track.title == "b"  # pas de boucle sur un titre illisible


async def test_join_and_leave(env):
    alice = env.m["Alice"]
    assert "Rejoignez d'abord un salon vocal" in await call(env, alice, "join")
    join(alice, VOICE)
    assert f"<#{VOICE_ID}>" in await call(env, alice, "join")
    assert env.backend.connected == {GUILD_ID: VOICE_ID} and env.bot.music.snapshot(GUILD_ID).current is None
    assert "déjà dans votre salon" in await call(env, alice, "join")
    join(env.m["Bob"], VOICE2)
    assert "rejoignez ce salon" in await call(env, env.m["Bob"], "join")
    assert "quitté le salon vocal" in await call(env, alice, "leave")


async def test_server_mute_is_reported_after_play(env):
    GUILD.me.voice = SimpleNamespace(channel=VOICE, mute=True, suppress=False)
    try:
        join(env.m["Alice"], VOICE)
        env.backend.results["ytmsearch:a"] = LoadResult([raw_track("a")])
        interaction = await run_play(env, env.m["Alice"], "a")
        assert "muet par le serveur" in all_embeds(interaction)
    finally:
        GUILD.me.voice = None


async def test_two_guilds_have_separate_queues_and_panels(env):
    other = FakeGuild(OTHER_GUILD)
    other_voice = FakeVoiceChannel(301)
    other.channels = {301: other_voice}
    other_text = FakeTextChannel(300, other)
    env.bot.get_channel = {TEXT_ID: env.text, 300: other_text}.get
    await start_session(env, "a", "b")
    dana = FakeMember(444444444444444444, "Dana")
    dana.voice = SimpleNamespace(channel=other_voice)
    other_voice.members.append(dana)
    env.backend.results["ytmsearch:z"] = LoadResult([raw_track("z", identifier="z")])
    interaction = FakeInteraction(other, dana)
    interaction.channel_id = 300
    await env.cog.play.callback(env.cog, interaction, "z", None)
    await flush(env.cog)
    assert env.backend.connected == {GUILD_ID: VOICE_ID, OTHER_GUILD: 301}
    assert env.bot.music.snapshot(OTHER_GUILD).current.track.title == "z"
    assert env.bot.music.snapshot(GUILD_ID).current.track.title == "a"
    assert len(other_text.messages) == 1 and len(env.text.messages) == 1  # un panneau par serveur
    # Arrêter sur un serveur ne touche jamais l'autre.
    await call(env, dana, "stop", guild=other)
    assert env.bot.music.snapshot(OTHER_GUILD).current is None
    assert env.bot.music.snapshot(GUILD_ID).current.track.title == "a"
    # Une personne d'un autre serveur ne peut pas agir sur ce lecteur, même avec le bon identifiant de bouton.
    message = panel(env)
    refused = await press(env, dana, click_ids(message)["stop"], guild=other)
    assert "n'appartient pas à ce serveur" in all_embeds(refused)


async def test_audiocheck_shows_the_five_steps(env):
    await start_session(env, "a")
    service = env.bot.music
    entry = service.snapshot(GUILD_ID).current
    from src.music.service import LiveVoiceState

    env.backend.live[GUILD_ID] = LiveVoiceState(exists=True, connected=False, ping_ms=-1, has_track=True, voice_sent=True,
                                                channel_sent=True)
    service.on_track_started(GUILD_ID, entry.entry_id)
    text = await call(env, env.m["Alice"], "audiocheck")
    for step in ("Recherche", "Piste chargée", "Lecteur démarré", "Connexion vocale", "Audio envoyé"):
        assert step in text
    assert ("live", GUILD_ID) in env.backend.calls
    assert "token" not in text.casefold() and "jeton vocal :" not in text.casefold()


async def test_audiocheck_without_music_session(env):
    text = await call(env, env.m["Alice"], "audiocheck")
    assert "Recherche" in text and env.backend.count("live") == 0


async def test_wavelink_audio_events_feed_the_diagnostic(env):
    await start_session(env, "a")
    service = env.bot.music
    entry = service.snapshot(GUILD_ID).current
    player = SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID))
    track = SimpleNamespace(extras={ENTRY_KEY: entry.entry_id})
    await env.cog.on_wavelink_track_start(SimpleNamespace(player=player, track=track))
    await env.cog.on_wavelink_player_update(SimpleNamespace(player=player, connected=True, ping=12, position=0))
    assert service.audio.trace(GUILD_ID).started_at is not None and service.audio.trace(GUILD_ID).voice_connected
    await env.cog.on_wavelink_websocket_closed(SimpleNamespace(player=player, code=4017, reason="DAVE", by_remote=True))
    await flush(env.cog)
    assert "DAVE" in panel(env).embed.description or "DAVE" in " ".join(f.value for f in panel(env).embed.fields)
    await env.cog.on_wavelink_stats_update(SimpleNamespace(frames=SimpleNamespace(sent=3000, nulled=0, deficit=0)))
    assert service.audio.frames == (3000, 0, 0)
    # Événements sans lecteur connu : ignorés sans erreur.
    await env.cog.on_wavelink_player_update(SimpleNamespace(player=None, connected=False, ping=-1, position=0))
    await env.cog.on_wavelink_track_start(SimpleNamespace(player=None, track=None))


async def test_audio_check_is_scheduled_after_a_track_starts(env):
    import asyncio

    env.cog.audio_check_delay = 0.01
    seen = []
    env.bot.music.check_audio = lambda guild_id, entry_id: seen.append((guild_id, entry_id))
    await start_session(env, "a")
    await asyncio.sleep(0.05)
    assert seen and seen[0][0] == GUILD_ID


async def test_expired_interaction_is_logged_not_raised(env, caplog):
    class Expired(FakeResponse):
        async def send_message(self, content=None, **kwargs):
            raise discord.NotFound(_Response(), "Unknown interaction")

    interaction = FakeInteraction(GUILD, env.m["Alice"])
    interaction.response = Expired()
    await env.cog.queue.callback(env.cog, interaction)  # ne lève rien
    assert "expiré" in caplog.text
