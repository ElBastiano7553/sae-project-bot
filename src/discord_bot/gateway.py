"""Implémentation Discord de ``MessageGateway`` (voir services.permanent_messages)."""

from __future__ import annotations

import logging

import discord

from src.discord_bot.embeds import payload_to_embed, payload_to_view
from src.services.content import MessagePayload, footer_for
from src.services.permanent_messages import GatewayError

log = logging.getLogger(__name__)

# Nombre de messages récents parcourus pour retrouver un message du bot si la base est perdue.
HISTORY_SCAN_LIMIT = 50


class DiscordMessageGateway:
    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def message_exists(self, channel_id: int, message_id: int) -> bool:
        channel = await self._text_channel(channel_id)
        try:
            await channel.fetch_message(message_id)
        except discord.NotFound:
            return False
        except discord.Forbidden as exc:
            raise GatewayError(f"accès refusé au salon #{channel.name} (lire l'historique)") from exc
        except discord.HTTPException as exc:
            raise GatewayError(f"erreur Discord ({exc.status}) en lisant #{channel.name}") from exc
        return True

    async def find_managed_message(self, channel_id: int, key: str) -> int | None:
        channel = await self._text_channel(channel_id)
        me = self._client.user
        marker = footer_for(key)
        try:
            async for message in channel.history(limit=HISTORY_SCAN_LIMIT):
                if me and message.author.id == me.id and any(e.footer.text == marker for e in message.embeds):
                    return message.id
        except discord.Forbidden as exc:
            raise GatewayError(f"accès refusé à l'historique de #{channel.name}") from exc
        except discord.HTTPException as exc:
            raise GatewayError(f"erreur Discord ({exc.status}) en lisant #{channel.name}") from exc
        return None

    async def send(self, channel_id: int, payload: MessagePayload) -> int:
        channel = await self._text_channel(channel_id)
        view = payload_to_view(payload)
        try:
            if view is None:
                message = await channel.send(embed=payload_to_embed(payload))
            else:
                message = await channel.send(embed=payload_to_embed(payload), view=view)
        except discord.Forbidden as exc:
            raise GatewayError(f"le bot ne peut pas écrire dans #{channel.name} (envoyer des messages / intégrer des liens)") from exc
        except discord.HTTPException as exc:
            raise GatewayError(f"erreur Discord ({exc.status}) en publiant dans #{channel.name} : {exc.text}") from exc
        return message.id

    async def edit(self, channel_id: int, message_id: int, payload: MessagePayload) -> None:
        channel = await self._text_channel(channel_id)
        try:
            await channel.get_partial_message(message_id).edit(
                embed=payload_to_embed(payload), view=payload_to_view(payload)
            )
        except discord.NotFound as exc:
            raise GatewayError("le message a été supprimé pendant la mise à jour : relancez /refresh") from exc
        except discord.Forbidden as exc:
            raise GatewayError(f"modification refusée dans #{channel.name}") from exc
        except discord.HTTPException as exc:
            raise GatewayError(f"erreur Discord ({exc.status}) en modifiant un message : {exc.text}") from exc

    async def pin(self, channel_id: int, message_id: int) -> None:
        channel = await self._text_channel(channel_id)
        try:
            await channel.get_partial_message(message_id).pin(reason="Message permanent du SAE Project Bot")
        except discord.HTTPException as exc:
            raise GatewayError(f"épinglage refusé dans #{channel.name}") from exc

    async def _text_channel(self, channel_id: int) -> discord.TextChannel:
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except discord.NotFound as exc:
                raise GatewayError(f"le salon {channel_id} n'existe plus : relancez /setup") from exc
            except discord.HTTPException as exc:
                raise GatewayError(f"salon {channel_id} inaccessible ({exc.status})") from exc
        if not isinstance(channel, discord.TextChannel):
            raise GatewayError(f"le salon {channel_id} n'est pas un salon textuel")
        return channel
