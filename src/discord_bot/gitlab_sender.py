"""Implémentation Discord de ``NotificationSender`` (voir gitlab.dispatcher).

Un salon supprimé, un salon qui n'est pas textuel ou une permission manquante lèvent
``SendRefused`` : la file abandonne alors la notification sans nouvel essai. Les autres
erreurs (Discord indisponible, réseau) restent retentées.
"""

from __future__ import annotations

import discord

from src.discord_bot.embeds import notification_to_embed
from src.gitlab.dispatcher import PermanentSendError
from src.gitlab.events import Notification
from src.services.permanent_messages import GatewayError


class SendRefused(GatewayError, PermanentSendError):
    """Refus définitif de Discord : réessayer ne servirait à rien."""


class DiscordNotificationSender:
    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def send_notification(self, channel_id: int, notification: Notification) -> None:
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise SendRefused(f"salon {channel_id} inaccessible ({exc.status}) : relancez /admin installer") from exc
            except discord.HTTPException as exc:
                raise GatewayError(f"salon {channel_id} inaccessible ({exc.status}) : relancez /admin installer") from exc
        if not isinstance(channel, discord.TextChannel):
            raise SendRefused(f"le salon {channel_id} n'est pas un salon textuel")
        try:
            # Aucune mention possible : un message de commit ne doit jamais notifier @everyone.
            await channel.send(embed=notification_to_embed(notification), allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden as exc:
            raise SendRefused(f"le bot ne peut pas écrire dans #{channel.name}") from exc
        except discord.NotFound as exc:
            raise SendRefused(f"le salon #{channel.name} n'existe plus") from exc
        except discord.HTTPException as exc:
            raise GatewayError(f"erreur Discord ({exc.status}) en publiant dans #{channel.name}") from exc
