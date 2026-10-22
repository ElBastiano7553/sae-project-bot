"""Conversion d'un serveur discord.py en instantané neutre (voir services.server_structure).

Lecture seule : aucune fonction de ce module ne modifie le serveur.
"""

from __future__ import annotations

import discord

from src.config import PERMISSION_LABELS
from src.services.server_redesign import DANGEROUS_PERMISSIONS
from src.services.server_structure import GuildCategory, GuildChannel, GuildRole, GuildSnapshot, Overwrite


def snapshot_guild(guild: discord.Guild) -> GuildSnapshot:
    me = guild.me
    default_role = guild.default_role
    categories = tuple(
        GuildCategory(
            c.id,
            c.name,
            c.position,
            manageable=_can_manage(c, me),
            everyone_can_view=bool(c.permissions_for(default_role).view_channel) if default_role is not None else True,
            overwrites=_overwrites(guild, c),
        )
        for c in guild.categories
    )
    channels = tuple(
        GuildChannel(
            ch.id,
            ch.name,
            channel_type(ch),
            ch.category_id,
            ch.position,
            manageable=_can_manage(ch, me),
            topic=getattr(ch, "topic", None) or "",
            overwrites=_overwrites(guild, ch),
            # Sans « last_message_id », Discord n'a jamais vu de message dans le salon.
            has_messages=getattr(ch, "last_message_id", 0) is not None,
        )
        for ch in guild.channels
        if not is_category(ch)
    )
    roles = tuple(
        GuildRole(
            role.id,
            role.name,
            role.position,
            managed=role.managed,
            is_default=role.is_default(),
            dangerous=tuple(name for name in DANGEROUS_PERMISSIONS if getattr(role.permissions, name, False)),
            color=role.colour.value,
            hoist=role.hoist,
            mentionable=role.mentionable,
            permissions=permission_names(role.permissions),
        )
        for role in guild.roles
    )
    bot_permissions = permission_names(me.guild_permissions) if me is not None else frozenset()
    return GuildSnapshot(
        categories=categories,
        channels=channels,
        roles=roles,
        bot_permissions=bot_permissions,
        bot_top_role_position=me.top_role.position if me is not None else 0,
        guild_id=guild.id,
        bot_id=me.id if me is not None else 0,
        owner_id=getattr(guild, "owner_id", None) or 0,
        community="COMMUNITY" in getattr(guild, "features", ()),
    )


def permission_names(permissions: discord.Permissions) -> frozenset[str]:
    """Noms des permissions accordées, un seul nom par permission.

    discord.py donne les noms canoniques (« read_messages ») alors que la
    configuration utilise ceux de l'interface Discord (« view_channel ») :
    quand une permission a plusieurs noms, on garde celui que la configuration connaît.
    """
    names = set()
    for flag in {flag for name, flag in discord.Permissions.VALID_FLAGS.items() if getattr(permissions, name, False)}:
        aliases = sorted(name for name, value in discord.Permissions.VALID_FLAGS.items() if value == flag)
        known = [name for name in aliases if name in KNOWN_PERMISSIONS]
        names.add(known[0] if known else min(aliases, key=lambda n: (n not in _CANONICAL, n)))
    return frozenset(names)


KNOWN_PERMISSIONS = frozenset(PERMISSION_LABELS) | frozenset(DANGEROUS_PERMISSIONS)
_CANONICAL = frozenset(name for name, _ in discord.Permissions.none())


def _overwrites(guild: discord.Guild, channel: discord.abc.GuildChannel) -> tuple[Overwrite, ...]:
    result = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        result.append(
            Overwrite(
                target.id,
                "role" if guild.get_role(target.id) is not None else "member",
                permission_names(allow),
                permission_names(deny),
            )
        )
    return tuple(sorted(result, key=lambda o: (o.target_type, o.target_id)))


def _can_manage(channel: discord.abc.GuildChannel, me: discord.Member | None) -> bool:
    if me is None:
        return False
    return bool(channel.permissions_for(me).manage_channels)


_TYPES = {
    discord.ChannelType.text: "text",
    discord.ChannelType.news: "announcement",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.media: "media",
}


def channel_type(channel: discord.abc.GuildChannel) -> str:
    return _TYPES.get(getattr(channel, "type", None), "other")


def is_category(channel: discord.abc.GuildChannel) -> bool:
    return getattr(channel, "type", None) == discord.ChannelType.category
