"""Envoi des notifications sur Discord, avec un faux salon (aucune connexion)."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.discord_bot.gitlab_sender import DiscordNotificationSender
from src.gitlab.events import format_push
from src.gitlab.rules import GitLabRules
from src.gitlab.samples import push_payload
from src.services.permanent_messages import GatewayError

RULES = GitLabRules(routes={"push": "a", "merge_request": "a", "pipeline": "b"})


def fake_client(channel):
    client = MagicMock()
    client.get_channel.return_value = channel
    return client


async def test_notifications_are_sent_with_all_mentions_disabled():
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    payload = push_payload()
    payload["commits"][1]["title"] = "@everyone <@&123> regardez"
    await DiscordNotificationSender(fake_client(channel)).send_notification(1, format_push(payload, RULES))

    kwargs = channel.send.await_args.kwargs
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False
    assert "content" not in kwargs  # seulement un embed : Discord ne notifie jamais depuis un embed
    assert "@everyone" not in kwargs["embed"].description


async def test_forbidden_channel_raises_gateway_error():
    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "gitlab-activite"
    channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "interdit"))
    with pytest.raises(GatewayError, match="ne peut pas écrire"):
        await DiscordNotificationSender(fake_client(channel)).send_notification(1, format_push(push_payload(), RULES))


async def test_voice_channel_is_refused():
    with pytest.raises(GatewayError, match="textuel"):
        await DiscordNotificationSender(fake_client(MagicMock(spec=discord.VoiceChannel))).send_notification(
            1, format_push(push_payload(), RULES)
        )
