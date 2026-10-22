"""Commandes d'IA (lot 3) : /ask, /summary, /review.

Ouvertes à tous les membres, réponses éphémères. Désactivées proprement si
AI_PROVIDER=none ; /review demande en plus l'API GitLab en lecture seule.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.ai import AIError
from src.config import load_link_catalog, load_planning
from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.embeds import ai_answer_embed
from src.gitlab.api import GitLabAPIError
from src.services.ai_assistant import AIAssistant, QuotaExceeded
from src.services.content import load_message_templates
from src.services.history import PERIOD_LABELS, compute_stats, period_start
from src.services.knowledge import load_passages, search

log = logging.getLogger(__name__)

SUMMARY_PERIODS = [app_commands.Choice(name=PERIOD_LABELS[key], value=key) for key in ("24h", "7j", "30j")]


class AICog(commands.Cog):
    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    async def _assistant(self, interaction: discord.Interaction) -> AIAssistant | None:
        """L'assistant si l'IA est active et la demande autorisée ; sinon répond à l'utilisateur."""
        assistant = self.bot.assistant
        if assistant is None:
            reason = (
                "la configuration de l'IA est invalide (voir `/status`)"
                if self.bot.settings.ai_config_error
                else "AI_PROVIDER=none dans la configuration du bot"
            )
            await send_error(interaction, f"L'IA est désactivée : {reason}.", title="IA indisponible", tone="warning")
            return None
        try:
            assistant.check_allowed(interaction.user.id)
        except QuotaExceeded as exc:
            await send_error(interaction, str(exc), title="Demande refusée", tone="warning")
            return None
        return assistant

    async def _reply_error(self, interaction: discord.Interaction, exc: Exception) -> None:
        await send_error(interaction, str(exc), title="L'IA n'a pas pu répondre", tone="warning")

    @app_commands.command(name="ask", description="Poser une question sur le projet (réponse de l'IA à partir des documents de l'équipe)")
    @app_commands.describe(question="Votre question (500 caractères au plus)")
    @app_commands.guild_only()
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 3, 500]) -> None:
        assistant = await self._assistant(interaction)
        if assistant is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        passages = search(
            load_passages(planning=load_planning(), catalog=load_link_catalog(), templates=load_message_templates()), question
        )
        try:
            answer = await assistant.ask(interaction.user.id, question, passages)
        except AIError as exc:
            await self._reply_error(interaction, exc)
            return
        await interaction.followup.send(embed=ai_answer_embed(answer, title="Réponse", subject=question), ephemeral=True)

    @app_commands.command(name="summary", description="Bilan de l'activité GitLab rédigé par l'IA (événements reçus par le bot)")
    @app_commands.describe(periode="Période (7 jours par défaut)")
    @app_commands.choices(periode=SUMMARY_PERIODS)
    @app_commands.guild_only()
    async def summary(self, interaction: discord.Interaction, periode: app_commands.Choice[str] | None = None) -> None:
        assistant = await self._assistant(interaction)
        if assistant is None:
            return
        period = periode.value if periode else "7j"
        await interaction.response.defer(ephemeral=True, thinking=True)
        since = period_start(period, dt.datetime.now(dt.UTC))
        records = self.bot.gitlab_events_repo.in_period(since)
        try:
            answer = await assistant.summarize(interaction.user.id, records, compute_stats(records, since), PERIOD_LABELS[period])
        except AIError as exc:
            await self._reply_error(interaction, exc)
            return
        embed = ai_answer_embed(answer, title=f"Bilan GitLab : {PERIOD_LABELS[period]}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="review", description="Relecture d'une merge request GitLab par l'IA (aide, pas une validation)")
    @app_commands.describe(mr="Numéro de la merge request (le n de !n)")
    @app_commands.guild_only()
    async def review(self, interaction: discord.Interaction, mr: app_commands.Range[int, 1, 1_000_000]) -> None:
        if self.bot.gitlab_api is None:
            await send_error(
                interaction,
                "/review lit la merge request sur GitLab : il faut GITLAB_ACCESS_TOKEN (scope read_api) et "
                "GITLAB_PROJECT_ID dans la configuration du bot (voir docs/ai.md).",
                title="API GitLab non configurée",
                tone="warning",
            )
            return
        assistant = await self._assistant(interaction)
        if assistant is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            info = await self.bot.gitlab_api.merge_request(mr)
            diffs = await self.bot.gitlab_api.merge_request_diffs(mr)
            answer = await assistant.review(interaction.user.id, info, diffs)
        except (GitLabAPIError, AIError) as exc:
            await self._reply_error(interaction, exc)
            return
        embed = ai_answer_embed(answer, title=f"Relecture de !{info.iid} · {info.title}", url=info.web_url)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(AICog(bot))
