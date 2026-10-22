"""Envoi des alertes d'erreurs répétées dans le salon ALERTS_CHANNEL.

Les problèmes d'envoi sont journalisés en WARNING (jamais en ERROR) : une alerte
impossible à publier ne doit pas elle-même produire de nouvelles alertes.
"""

from __future__ import annotations

import logging

import discord

from src.discord_bot.embeds import alert_embed
from src.services.alerts import Alert

log = logging.getLogger(__name__)


async def send_alert(client: discord.Client, channel_id: int, alert: Alert) -> bool:
    channel = client.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning("Alerte non publiée : le salon %s est introuvable ou n'est pas textuel", channel_id)
        return False
    try:
        await channel.send(embed=alert_embed(alert), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        log.warning("Alerte non publiée dans #%s : erreur Discord %s", channel.name, exc.status)
        return False
    return True
