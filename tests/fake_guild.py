"""Faux serveur Discord en mémoire pour tester /refonte sans réseau ni vrai serveur.

Il imite ce que Samae utilise : catégories et salons (types, positions, sujets,
permissions particulières, dernier message), rôles (couleur, hiérarchie,
permissions), membre du bot, créations, modifications, suppressions, appel
groupé des positions. Le vrai ``snapshot_guild`` fonctionne dessus.
Chaque appel modifiant le serveur est noté dans ``guild.calls`` ; une panne
peut être programmée avec ``guild.fail(action, nom, exception)``.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

import discord

from src.discord_bot.snapshot import snapshot_guild

GUILD_ID = 42
BOT_ID = 999
_ids = itertools.count(900000)
TYPES = {
    "category": discord.ChannelType.category,
    "text": discord.ChannelType.text,
    "announcement": discord.ChannelType.news,
    "voice": discord.ChannelType.voice,
    "stage": discord.ChannelType.stage_voice,
    "forum": discord.ChannelType.forum,
    "media": discord.ChannelType.media,
}
VOICE_TYPES = {discord.ChannelType.voice, discord.ChannelType.stage_voice}


class FakeRole:
    def __init__(self, guild, role_id, name, position, *, default=False, managed=False, color=0, permissions=None,
                 hoist=False, mentionable=False) -> None:
        self.guild, self.id, self.name, self.position = guild, role_id, name, position
        self.managed, self._default = managed, default
        self.colour = discord.Colour(color)
        self.permissions = permissions or discord.Permissions.none()
        self.hoist, self.mentionable = hoist, mentionable

    def is_default(self) -> bool:
        return self._default

    async def edit(self, *, reason=None, **changes):
        self.guild._record("edit_role", self.name, changes)
        if "name" in changes:
            self.name = changes["name"]
        if "colour" in changes:
            self.colour = discord.Colour(changes["colour"].value)
        for flag in ("hoist", "mentionable"):
            if flag in changes:
                setattr(self, flag, changes[flag])
        if "permissions" in changes:
            self.permissions = discord.Permissions(changes["permissions"].value)
        return self

    async def delete(self, *, reason=None):
        self.guild._record("delete_role", self.name, {})
        self.guild.roles.remove(self)

    def __repr__(self) -> str:
        return f"<FakeRole {self.name}>"


class FakeBotMember:
    """Samae dans le serveur : clé des permissions particulières, rôle le plus haut, permissions globales."""

    def __init__(self, guild, top_role) -> None:
        self.guild, self.id, self.top_role = guild, BOT_ID, top_role

    @property
    def guild_permissions(self) -> discord.Permissions:
        return discord.Permissions(**{name: True for name in self.guild.bot_permissions})

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other) -> bool:
        return getattr(other, "id", None) == self.id


class FakeChannel:
    def __init__(self, guild, channel_id, name, kind, category_id=None, *, position=0, topic="", has_messages=True) -> None:
        self.guild, self.id, self.name, self.type = guild, channel_id, name, TYPES[kind]
        self.category_id, self.position, self.topic = category_id, position, topic
        self.overwrites: dict = {}
        self.last_message_id = 1 if has_messages else None

    @property
    def category(self):
        return self.guild.get_channel(self.category_id) if self.category_id else None

    @property
    def channels(self) -> list:
        return [ch for ch in self.guild._channels.values() if ch.category_id == self.id]

    def permissions_for(self, member) -> discord.Permissions:
        if member is self.guild.me:
            perms = discord.Permissions(**{name: True for name in self.guild.bot_permissions})
            if "administrator" in self.guild.bot_permissions:
                return discord.Permissions.all()
        else:
            perms = discord.Permissions.general()
        overwrite = self.overwrites.get(member)
        if overwrite is not None:
            allow, deny = overwrite.pair()
            perms = discord.Permissions((perms.value & ~deny.value) | allow.value)
        return perms

    async def edit(self, *, reason=None, **changes):
        self.guild._record("edit", self.name, changes)
        if "sync_permissions" in changes:
            assert changes["sync_permissions"] is False, "un déplacement ne doit jamais écraser les permissions"
        if "name" in changes:
            self.name = changes["name"]
        if "topic" in changes:
            self.topic = changes["topic"]
        if "type" in changes:
            self.type = changes["type"]
        if "category" in changes:
            self.category_id = changes["category"].id if changes["category"] else None
        if "overwrites" in changes:
            self.overwrites = dict(changes["overwrites"])
        return self

    async def delete(self, *, reason=None):
        self.guild._record("delete", self.name, {})
        self.guild._channels.pop(self.id)

    def __repr__(self) -> str:
        return f"<FakeChannel {self.name}>"


class FakeHTTP:
    def __init__(self, guild) -> None:
        self.guild = guild

    async def bulk_channel_update(self, guild_id, data, *, reason=None):
        self.guild._record("bulk_positions", "", {"data": data})
        for item in data:
            channel = self.guild._channels[item["id"]]
            channel.position = item["position"]
            if "parent_id" in item:
                channel.category_id = item["parent_id"]


class FakeGuild:
    def __init__(self, *, community: bool = False, bot_permissions: set[str] | None = None) -> None:
        self.id = GUILD_ID
        self.owner_id = 1
        self.features = ["COMMUNITY"] if community else []
        self.calls: list[tuple] = []
        self._failures: dict[tuple[str, str], BaseException] = {}
        self.delays: dict[tuple[str, str], float] = {}
        self._channels: dict[int, FakeChannel] = {}
        self.default_role = FakeRole(self, GUILD_ID, "@everyone", 0, default=True)
        bot_role = FakeRole(self, 2, "Samae", 10, managed=True)
        self.roles = [self.default_role, bot_role]
        self.bot_permissions = set(bot_permissions if bot_permissions is not None else {"manage_channels", "manage_roles", "view_channel",
                                                                                         "send_messages", "embed_links", "read_message_history",
                                                                                         "add_reactions", "manage_messages", "manage_threads",
                                                                                         "manage_events", "pin_messages", "create_public_threads",
                                                                                         "create_private_threads"})
        self.me = FakeBotMember(self, bot_role)
        self._state = SimpleNamespace(http=FakeHTTP(self))

    # --- Construction du scénario ------------------------------------------------------------

    def add_category(self, name: str) -> FakeChannel:
        position = max((c.position for c in self.categories), default=-1) + 1
        category = FakeChannel(self, next(_ids), name, "category", position=position)
        self._channels[category.id] = category
        return category

    def add_channel(self, name: str, category: FakeChannel | None = None, kind: str = "text", *, topic: str = "",
                    has_messages: bool = True) -> FakeChannel:
        siblings = [c for c in self._channels.values() if c.category_id == (category.id if category else None)]
        position = max((c.position for c in siblings), default=-1) + 1
        channel = FakeChannel(self, next(_ids), name, kind, category.id if category else None, position=position, topic=topic,
                              has_messages=has_messages)
        self._channels[channel.id] = channel
        return channel

    def add_role(self, name: str, *, color: int = 0, permissions: discord.Permissions | None = None, position: int | None = None,
                 managed: bool = False) -> FakeRole:
        if position is None:
            for role in self.roles:
                if not role.is_default():
                    role.position += 1
            position = 1
        role = FakeRole(self, next(_ids), name, position, managed=managed, color=color, permissions=permissions)
        self.roles.append(role)
        return role

    def fail(self, action: str, name: str, error: BaseException) -> None:
        self._failures[(action, name)] = error

    def _record(self, action: str, name: str, details: dict) -> None:
        self.calls.append((action, name, details))
        error = self._failures.pop((action, name), None) or self._failures.pop((action, "*"), None)
        if error is not None:
            raise error

    # --- API discord.py utilisée par Samae ------------------------------------------------------

    @property
    def categories(self) -> list[FakeChannel]:
        return sorted((c for c in self._channels.values() if c.type == discord.ChannelType.category), key=lambda c: (c.position, c.id))

    @property
    def channels(self) -> list[FakeChannel]:
        return list(self._channels.values())

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_member(self, member_id):
        return self.me if member_id == BOT_ID else None

    async def create_category(self, name, *, overwrites=None, reason=None, position=None):
        self._record("create_category", name, {"overwrites": overwrites})
        category = self.add_category(name)
        category.overwrites = dict(overwrites or {})
        return category

    async def _create(self, name, kind, category, overwrites, topic=""):
        self._record("create_channel", name, {"kind": kind, "overwrites": overwrites})
        delay = self.delays.get(("create_channel", name))
        if delay:
            await asyncio.sleep(delay)
        channel = self.add_channel(name, category, kind, topic=topic or "", has_messages=False)
        channel.overwrites = dict(overwrites or {})
        return channel

    async def create_text_channel(self, name, *, category=None, overwrites=None, reason=None, topic="", news=False):
        return await self._create(name, "announcement" if news else "text", category, overwrites, topic)

    async def create_voice_channel(self, name, *, category=None, overwrites=None, reason=None):
        return await self._create(name, "voice", category, overwrites)

    async def create_stage_channel(self, name, *, category=None, overwrites=None, reason=None):
        assert self.features, "une conférence demande la fonction Communauté"
        return await self._create(name, "stage", category, overwrites)

    async def create_forum(self, name, *, category=None, overwrites=None, reason=None, topic="", media=False):
        return await self._create(name, "media" if media else "forum", category, overwrites, topic)

    async def create_role(self, *, name, permissions, colour, hoist, mentionable, reason=None):
        self._record("create_role", name, {"permissions": permissions, "colour": colour})
        assert not permissions.administrator, "Samae ne donne jamais la permission Administrateur"
        role = self.add_role(name, color=colour.value, permissions=permissions)
        role.hoist, role.mentionable = hoist, mentionable
        return role

    async def edit_role_positions(self, positions, *, reason=None):
        self._record("role_positions", "", {"positions": {r.name: p for r, p in positions.items()}})
        moved = set()
        for role, position in positions.items():
            assert position < self.me.top_role.position, "Samae ne peut pas placer un rôle au-dessus du sien"
            role.position = position
            moved.add(role.id)
        others = sorted((r for r in self.roles if not r.is_default()), key=lambda r: (r.position, r.id in moved, r.id))
        for index, role in enumerate(others, start=1):
            role.position = index

    # --- Aides de test ------------------------------------------------------------------------------

    def snapshot(self):
        return snapshot_guild(self)

    def by_name(self, name: str) -> FakeChannel | None:
        return next((c for c in self._channels.values() if c.name == name), None)

    def role(self, name: str) -> FakeRole | None:
        return next((r for r in self.roles if r.name == name), None)

    def tree(self) -> dict[str, list[str]]:
        """Catégories dans l'ordre d'affichage, avec leurs salons (textes puis vocaux)."""
        result = {}
        for category in self.categories:
            members = sorted(category.channels, key=lambda c: (c.type in VOICE_TYPES, c.position, c.id))
            result[category.name] = [c.name for c in members]
        orphans = [c.name for c in self._channels.values() if c.category_id is None and c.type != discord.ChannelType.category]
        if orphans:
            result[""] = orphans
        return result

    def mutations(self) -> list[tuple]:
        return list(self.calls)


def sae_server(**kwargs) -> FakeGuild:
    """Le serveur de l'équipe avant toute refonte (noms d'origine, catégories numérotées)."""
    guild = FakeGuild(**kwargs)
    layout = {
        "01 · ACCUEIL & ORGANISATION": ["bienvenue", "annonces", "planning-et-deadlines", "discussion-generale", "decisions-et-comptes-rendus"],
        "02 · CONCEPTION DU JEU": ["idees-et-game-design", "gameplay-et-ia", "assets-et-audio"],
        "03 · DÉVELOPPEMENT": ["developpement-python", "reseau-multijoueur", "bugs-et-tests", "code-review"],
        "04 · GITLAB AUTOMATISÉ": ["gitlab-activite", "builds-et-tests", "bilan-ia"],
        "05 · LIVRABLES SAE": ["documentation-et-preuves", "preparation-revues"],
    }
    for category_name, names in layout.items():
        category = guild.add_category(category_name)
        for name in names:
            guild.add_channel(name, category)
    vocal = guild.add_category("06 · VOCAL")
    guild.add_channel("reunion-equipe", vocal, "voice")
    guild.add_channel("travail-en-commun", vocal, "voice")
    return guild
