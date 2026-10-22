"""Conversion d'un serveur discord.py en instantané neutre (voir services.server_structure).

Lecture seule : aucune fonction de ce module ne modifie le serveur.
"""

from __future__ import annotations

import discord

from src.services.server_redesign import DANGEROUS_PERMISSIONS
from src.services.server_structure import GuildCategory, GuildChannel, GuildRole, GuildSnapshot


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
        )
        for c in guild.categories
    )
    channels = tuple(
        GuildChannel(ch.id, ch.name, _channel_type(ch), ch.category_id, ch.position, manageable=_can_manage(ch, me))
        for ch in guild.channels
        if not isinstance(ch, discord.CategoryChannel)
    )
    roles = tuple(
        GuildRole(
            role.id,
            role.name,
            role.position,
            managed=role.managed,
            is_default=role.is_default(),
            dangerous=tuple(name for name in DANGEROUS_PERMISSIONS if getattr(role.permissions, name, False)),
        )
        for role in guild.roles
    )
    bot_permissions = frozenset(name for name, value in me.guild_permissions if value) if me is not None else frozenset()
    return GuildSnapshot(
        categories=categories,
        channels=channels,
        roles=roles,
        bot_permissions=bot_permissions,
        bot_top_role_position=me.top_role.position if me is not None else 0,
    )


def _can_manage(channel: discord.abc.GuildChannel, me: discord.Member | None) -> bool:
    if me is None:
        return False
    return bool(channel.permissions_for(me).manage_channels)


def _channel_type(channel: discord.abc.GuildChannel) -> str:
    if isinstance(channel, discord.TextChannel):
        return "text"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    return "other"
