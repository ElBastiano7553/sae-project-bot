"""Conversion d'un serveur discord.py en instantané neutre (voir services.server_structure)."""

from __future__ import annotations

import discord

from src.services.server_structure import GuildCategory, GuildChannel, GuildSnapshot


def snapshot_guild(guild: discord.Guild) -> GuildSnapshot:
    categories = tuple(GuildCategory(c.id, c.name) for c in guild.categories)
    channels = tuple(
        GuildChannel(ch.id, ch.name, _channel_type(ch), ch.category_id)
        for ch in guild.channels
        if not isinstance(ch, discord.CategoryChannel)
    )
    return GuildSnapshot(categories=categories, channels=channels)


def _channel_type(channel: discord.abc.GuildChannel) -> str:
    if isinstance(channel, discord.TextChannel):
        return "text"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    return "other"
