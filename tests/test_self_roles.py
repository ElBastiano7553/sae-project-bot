"""Choix des rôles (/roles) et accueil des nouveaux membres : sécurité, menu, message d'arrivée."""

from __future__ import annotations

import datetime as dt

import discord
import pytest

from src.config import ConfigError, Settings, load_server_layout
from src.discord_bot.bot import EXTENSIONS, SaeBot
from src.services.self_roles import ROLE_PICKER_ID, build_catalog, plan_change
from src.services.welcome import PENDING_KEY, PendingWelcome, dump_pending, load_pending, split_due
from tests.fake_guild import GUILD_ID, FakeGuild

LAYOUT = load_server_layout()


def team_guild(**kwargs) -> FakeGuild:
    """Serveur avec les rôles de domaine de config/server.toml, un rôle de staff et un rôle sensible."""
    guild = FakeGuild(**kwargs)
    for name in ("Développement", "Game Design", "Art & Design", "Testeur", "Notif · Annonces", "Responsable projet"):
        guild.add_role(name)
    return guild


# --- Catalogue : quels rôles un membre peut choisir ------------------------------------------------


def test_only_self_assignable_roles_are_offered():
    guild = team_guild()
    catalog = build_catalog(LAYOUT, guild.snapshot())
    names = [role.name for role in catalog.roles]
    assert names == ["Développement", "Game Design", "Art & Design", "Testeur", "Notif · Annonces"]
    assert "Responsable projet" not in names  # rôle de staff : jamais proposé
    assert catalog.bot_can_manage
    # Notif · Réunions est facultatif et absent : signalé, pas inventé.
    assert any(p.key == "notif_reunions" and "facultatif" in p.reason for p in catalog.problems)
    groups = dict(catalog.grouped())
    assert [r.name for r in groups["Notifications"]] == ["Notif · Annonces"]


def test_role_with_sensitive_permission_is_refused_even_if_marked():
    guild = team_guild()
    guild.role("Développement").permissions = discord.Permissions(manage_roles=True, mention_everyone=True)
    catalog = build_catalog(LAYOUT, guild.snapshot())
    assert "Développement" not in [role.name for role in catalog.roles]
    problem = next(p for p in catalog.problems if p.key == "developpement")
    assert "Gérer les rôles" in problem.reason and "Mentionner @everyone" in problem.reason


def test_role_above_samae_or_managed_is_refused():
    guild = team_guild()
    guild.role("Testeur").position = 50  # au-dessus du rôle de Samae (10)
    catalog = build_catalog(LAYOUT, guild.snapshot())
    assert "Testeur" not in [role.name for role in catalog.roles]
    assert "au-dessus du rôle de Samae" in next(p.reason for p in catalog.problems if p.key == "testeur")


def test_bot_without_manage_roles_is_reported():
    guild = team_guild(bot_permissions={"view_channel", "send_messages"})
    assert not build_catalog(LAYOUT, guild.snapshot()).bot_can_manage


def test_plan_change_only_touches_offered_roles():
    guild = team_guild()
    catalog = build_catalog(LAYOUT, guild.snapshot())
    dev, design = guild.role("Développement").id, guild.role("Art & Design").id
    staff = guild.role("Responsable projet").id
    # Le membre a déjà Développement et le rôle de staff ; il choisit Art & Design (et tente le rôle de staff).
    change = plan_change(catalog, current_role_ids={dev, staff}, selected_ids=[design, staff, 123456])
    assert [r.name for r in change.add] == ["Art & Design"]
    assert [r.name for r in change.remove] == ["Développement"]
    # Le rôle de staff n'est ni ajouté ni retiré, quoi que contienne la requête.
    assert staff not in {r.role_id for r in (*change.add, *change.remove)}


def test_self_assignable_is_refused_on_staff_or_permission_roles(tmp_path):
    base = (LAYOUT_FILE := tmp_path / "server.toml")
    text = (
        '[categories.a]\nname = "A"\n[channels.x]\nname = "x"\ncategory = "a"\n'
        '[roles.chef]\nname = "Chef"\nkind = "staff"\nself_assignable = true\n'
    )
    base.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="self_assignable"):
        load_server_layout(LAYOUT_FILE)
    base.write_text(
        '[categories.a]\nname = "A"\n[channels.x]\nname = "x"\ncategory = "a"\n'
        '[roles.dev]\nname = "Dev"\nkind = "fonction"\npermissions = ["create_polls"]\nself_assignable = true\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="sans aucune permission"):
        load_server_layout(LAYOUT_FILE)


def test_invalid_fallback_is_a_config_error(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text('[categories.a]\nname = "A"\n[channels.x]\nname = "x"\ncategory = "a"\nfallback = "absent"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="fallback"):
        load_server_layout(path)


def test_welcome_settings():
    settings = Settings.from_env({"DISCORD_GUILD_ID": "42"}, require_token=False)
    assert not settings.welcome_on_join and settings.welcome_delete_hours == 24
    settings = Settings.from_env({"DISCORD_GUILD_ID": "42", "WELCOME_ON_JOIN": "true", "WELCOME_DELETE_AFTER_HOURS": "0"}, require_token=False)
    assert settings.welcome_on_join and settings.welcome_delete_hours == 0
    with pytest.raises(ConfigError, match="WELCOME_DELETE_AFTER_HOURS"):
        Settings.from_env({"DISCORD_GUILD_ID": "42", "WELCOME_DELETE_AFTER_HOURS": "1000"}, require_token=False)


# --- Menu de sélection (cog) ----------------------------------------------------------------------


class FakeMember:
    def __init__(self, guild, member_id: int = 7, roles=()) -> None:
        self.guild, self.id = guild, member_id
        self.roles = [guild.default_role, *roles]
        self.bot = False
        self.mention = f"<@{member_id}>"
        self.guild_permissions = discord.Permissions.none()
        self.added: list[str] = []
        self.removed: list[str] = []
        self.fail: Exception | None = None

    async def add_roles(self, *roles, reason=None):
        if self.fail:
            raise self.fail
        self.added += [r.name for r in roles]
        self.roles += list(roles)

    async def remove_roles(self, *roles, reason=None):
        if self.fail:
            raise self.fail
        self.removed += [r.name for r in roles]
        self.roles = [r for r in self.roles if r not in roles]


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deferred = False

    def is_done(self) -> bool:
        return bool(self.sent) or self.deferred

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})

    async def defer(self, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, client, guild, member) -> None:
        self.client, self.guild, self.user = client, guild, member
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edits: list[dict] = []
        self.data: dict = {}

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


@pytest.fixture
async def env(db, monkeypatch):
    settings = Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID)}, require_token=False)
    bot = SaeBot(settings, db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    guild = team_guild()
    monkeypatch.setattr(SaeBot, "target_guild", property(lambda self: guild))
    yield bot, bot.get_cog("WelcomeCog"), guild
    await bot.close()


async def test_roles_command_opens_a_private_menu_with_current_roles_checked(env):
    bot, cog, guild = env
    member = FakeMember(guild, roles=[guild.role("Testeur")])
    interaction = FakeInteraction(bot, guild, member)
    await cog.roles.callback(cog, interaction)
    sent = interaction.response.sent[0]
    assert sent["ephemeral"]
    options = sent["view"].select.options
    assert [o.label for o in options if o.default] == ["Testeur"]
    assert sent["view"].select.min_values == 0 and sent["view"].select.max_values == len(options)
    assert "Responsable projet" not in [o.label for o in options]


async def test_choosing_roles_adds_and_removes_only_offered_roles(env):
    bot, cog, guild = env
    staff = guild.role("Responsable projet")
    member = FakeMember(guild, roles=[guild.role("Testeur"), staff])
    interaction = FakeInteraction(bot, guild, member)
    selected = [guild.role("Développement").id, guild.role("Art & Design").id, staff.id]
    await cog.apply_choice(interaction, selected)
    assert member.added == ["Développement", "Art & Design"]
    assert member.removed == ["Testeur"]
    assert staff in member.roles  # jamais retiré, jamais « rajouté »
    result = interaction.edits[-1]["embeds"][0]
    assert result.title == "Rôles mis à jour" and "Développement" in result.description


async def test_discord_refusal_is_explained(env):
    bot, cog, guild = env
    member = FakeMember(guild)

    class _Response:
        status, reason = 403, "Forbidden"

    member.fail = discord.Forbidden(_Response(), "Missing Permissions")
    interaction = FakeInteraction(bot, guild, member)
    await cog.apply_choice(interaction, [guild.role("Développement").id])
    text = interaction.followup.sent[-1]["embed"].description
    assert "rôle de Samae" in text


async def test_role_picker_refuses_outside_the_team_server(env):
    bot, cog, guild = env
    other = FakeGuild()
    other.id = 1234
    interaction = FakeInteraction(bot, other, FakeMember(other))
    await cog.open_picker(interaction)
    assert "serveur de l'équipe" in interaction.response.sent[0]["embed"].description


async def test_role_picker_button_is_persistent(env):
    bot, cog, guild = env
    assert any(item.__name__ == "RolePickerButton" for item in bot._connection._view_store._dynamic_items.values())
    assert ROLE_PICKER_ID == "samae:roles:open"


# --- Message d'arrivée et suppression automatique --------------------------------------------------


class FakeTextChannel(discord.TextChannel):
    """Salon textuel minimal : reconnu par isinstance, enregistre les messages envoyés."""

    def __init__(self, channel_id: int) -> None:  # pas d'appel au constructeur de discord.py
        self.id = channel_id
        self.name = "bienvenue"
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return type("Message", (), {"id": 5000 + len(self.sent)})()


@pytest.fixture
async def welcome_env(db, monkeypatch):
    settings = Settings.from_env({"DISCORD_GUILD_ID": str(GUILD_ID), "WELCOME_ON_JOIN": "true"}, require_token=False)
    bot = SaeBot(settings, db)
    for extension in EXTENSIONS:
        await bot.load_extension(extension)
    guild = team_guild()
    monkeypatch.setattr(SaeBot, "target_guild", property(lambda self: guild))
    channel = FakeTextChannel(777)
    bot.bindings_repo.replace_all({"bienvenue": channel.id, "annonces": 778, "discussion": 779})
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel if channel_id == channel.id else None)
    deleted: list[tuple[int, int]] = []

    async def fake_delete(channel_id, message_id):
        deleted.append((channel_id, message_id))

    monkeypatch.setattr(bot.gateway, "delete", fake_delete)
    yield bot, bot.get_cog("WelcomeCog"), guild, channel, deleted
    await bot.close()


async def test_new_member_gets_a_welcome_with_role_button_then_it_is_deleted(welcome_env):
    bot, cog, guild, channel, deleted = welcome_env
    member = FakeMember(guild, member_id=55)
    member.guild = guild
    await cog.on_member_join(member)
    message = channel.sent[0]
    assert message["content"] == "<@55>"
    assert message["allowed_mentions"].users == [member] and not message["allowed_mentions"].everyone
    embed = message["embed"]
    assert "<@55>" in embed.description and "24 h" in embed.footer.text
    assert "<#778>" in embed.fields[0].value  # règlement : repli sur #annonces tant que #règlement n'existe pas
    assert [item.custom_id for item in message["view"].children] == [ROLE_PICKER_ID]
    pending = load_pending(bot.state_repo.get(PENDING_KEY))
    assert [(p.channel_id, p.member_id) for p in pending] == [(777, 55)]

    # Rien n'est supprimé avant l'échéance ; ensuite, seul ce message l'est.
    assert await cog.delete_due() == 0
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=25)
    assert await cog.delete_due(now=later) == 1
    assert deleted == [(777, pending[0].message_id)]
    assert load_pending(bot.state_repo.get(PENDING_KEY)) == []


async def test_welcome_is_removed_when_the_member_leaves(welcome_env):
    bot, cog, guild, channel, deleted = welcome_env
    other = PendingWelcome(777, 1, 99, "2999-01-01T00:00:00+00:00")
    bot.state_repo.set(PENDING_KEY, dump_pending([other]))
    member = FakeMember(guild, member_id=55)
    await cog.on_member_join(member)
    await cog.on_member_remove(member)
    assert len(deleted) == 1 and deleted[0][1] != 1  # le message de l'autre membre reste
    assert [p.member_id for p in load_pending(bot.state_repo.get(PENDING_KEY))] == [99]


async def test_bots_and_other_servers_are_not_welcomed(welcome_env):
    bot, cog, guild, channel, deleted = welcome_env
    robot = FakeMember(guild, member_id=56)
    robot.bot = True
    await cog.on_member_join(robot)
    stranger_guild = FakeGuild()
    stranger_guild.id = 1
    await cog.on_member_join(FakeMember(stranger_guild, member_id=57))
    assert channel.sent == []


async def test_welcome_is_disabled_by_default(env):
    bot, cog, guild = env
    assert not bot.intents.members
    await cog.on_member_join(FakeMember(guild, member_id=58))  # aucun salon associé, aucune erreur


def test_pending_list_survives_bad_data():
    assert load_pending("pas du json") == []
    now = dt.datetime(2026, 10, 24, tzinfo=dt.UTC)
    items = [PendingWelcome(1, 1, 1, "2026-10-23T00:00:00+00:00"), PendingWelcome(1, 2, 2, "2026-10-25T00:00:00+00:00")]
    due, keep = split_due(items, now)
    assert [i.message_id for i in due] == [1] and [i.message_id for i in keep] == [2]
