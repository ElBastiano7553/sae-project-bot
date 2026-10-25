"""Groupe /activite : résumé du projet, historique GitLab, statistiques et contributions.

Ouvertes à tous les membres, réponses éphémères. Tout est calculé à partir des
événements enregistrés par le bot (voir services.history) : rien n'est estimé.
"""

from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from src.discord_bot.bot import SaeBot, require_team_space, send_error
from src.config import load_link_catalog
from src.discord_bot.embeds import errors_embed, events_embed, leaderboard_embed, project_embed, stats_embed
from src.discord_bot.permissions import is_bot_admin
from src.gitlab.dispatcher import DEFAULT_BRANCH_KEY
from src.services.history import (
    PERIOD_LABELS,
    compute_leaderboard,
    compute_stats,
    period_start,
    previous_period,
    weekly_activity,
)

EVENT_LIMIT = 15
RECENT_EVENTS = 5
SEARCH_MAX = 100
ERRORS_TYPE = "errors"
EVENT_TYPES = {
    "push": "Pushs",
    "merge_request": "Merge requests",
    "pipeline": "Pipelines",
    "deployment": "Déploiements",
    ERRORS_TYPE: "Erreurs du bot (admins)",
}
PERIOD_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in PERIOD_LABELS.items()]
TYPE_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in EVENT_TYPES.items()]


class ActivityCog(commands.Cog):
    activite = app_commands.Group(
        name="activite", description="Activité des dépôts : résumé, événements reçus, statistiques, contributions", guild_only=True
    )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Espace d'équipe : utilisable seulement sur le serveur principal (voir /setup serveur-principal)."""
        return require_team_space(interaction)

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        return isinstance(member, discord.Member) and is_bot_admin(member, self.bot.settings.admin_role_name)

    @activite.command(name="resume", description="Résumé du projet : liens, branche principale, derniers événements GitLab")
    async def project(self, interaction: discord.Interaction) -> None:
        repo = self.bot.gitlab_events_repo
        default_branch = self.bot.state_repo.get(DEFAULT_BRANCH_KEY)
        embed = project_embed(
            catalog=load_link_catalog(),
            default_branch=default_branch,
            latest_pipeline=repo.latest_pipeline(default_branch) if default_branch else None,
            merge_requests=repo.recent(("merge_request",), RECENT_EVENTS),
            pipelines=repo.recent(("pipeline",), RECENT_EVENTS),
            pushes=repo.recent(("push",), RECENT_EVENTS),
            gitlab_enabled=self.bot.settings.gitlab_webhooks_enabled,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @activite.command(name="evenements", description="Historique des événements GitLab reçus (filtrable)")
    @app_commands.describe(type="Type d'événement", periode="Période (7 jours par défaut)", recherche="Texte cherché dans le titre, la branche ou l'auteur")
    @app_commands.choices(type=TYPE_CHOICES, periode=PERIOD_CHOICES)
    async def events(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] | None = None,
        periode: app_commands.Choice[str] | None = None,
        recherche: app_commands.Range[str, 1, SEARCH_MAX] | None = None,
    ) -> None:
        period = periode.value if periode else "7j"
        since = period_start(period, dt.datetime.now(dt.UTC))
        kind = type.value if type else None
        if kind == ERRORS_TYPE:
            if not self._is_admin(interaction):
                await send_error(interaction, "Les erreurs du bot sont réservées aux administrateurs du bot.", title="Accès refusé")
                return
            errors = self.bot.bot_errors_repo.recent(since, EVENT_LIMIT)
            if recherche:
                needle = recherche.casefold()
                errors = [e for e in errors if needle in e.message.casefold() or needle in e.source.casefold()]
            await interaction.response.send_message(embed=errors_embed(errors, period=PERIOD_LABELS[period]), ephemeral=True)
            return
        repo = self.bot.gitlab_events_repo
        records = repo.recent((kind,) if kind else (), EVENT_LIMIT, since=since, search=recherche)
        filters = " · ".join(
            part for part in (EVENT_TYPES[kind] if kind else "tous types", PERIOD_LABELS[period], f"« {recherche} »" if recherche else "") if part
        )
        embed = events_embed(records, filters=filters, coverage=repo.coverage(), limit=EVENT_LIMIT)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @activite.command(name="stats", description="Statistiques GitLab : commits, merge requests, pipelines, déploiements")
    @app_commands.describe(periode="Période (7 jours par défaut)")
    @app_commands.choices(periode=PERIOD_CHOICES)
    async def stats(self, interaction: discord.Interaction, periode: app_commands.Choice[str] | None = None) -> None:
        period = periode.value if periode else "7j"
        now = dt.datetime.now(dt.UTC)
        repo = self.bot.gitlab_events_repo
        coverage = repo.coverage()
        since = period_start(period, now)
        previous = previous_period(period, now)
        # La comparaison n'a de sens que si l'historique couvre toute la période précédente.
        compare = previous is not None and coverage[0] is not None and coverage[0] <= previous[0]
        records = repo.in_period(previous[0] if compare else since)
        embed = stats_embed(
            compute_stats(records, since),
            period=PERIOD_LABELS[period],
            previous=compute_stats(records, *previous) if compare else None,
            weekly=weekly_activity(repo.in_period(period_start("30j", now)), now) if period in {"30j", "90j", "tout"} else [],
            coverage=coverage,
            gitlab_enabled=self.bot.settings.gitlab_webhooks_enabled,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @activite.command(name="contributions", description="Contributions de l'équipe (MR fusionnées, approbations, jours actifs)")
    @app_commands.describe(periode="Période (30 jours par défaut)", details="Afficher aussi les MR ouvertes et les commits")
    @app_commands.choices(periode=PERIOD_CHOICES)
    async def leaderboard(
        self, interaction: discord.Interaction, periode: app_commands.Choice[str] | None = None, details: bool = False
    ) -> None:
        period = periode.value if periode else "30j"
        repo = self.bot.gitlab_events_repo
        records = repo.in_period(period_start(period, dt.datetime.now(dt.UTC)), ("push", "merge_request"))
        board = compute_leaderboard(records, repo.user_names())
        await interaction.response.send_message(
            embed=leaderboard_embed(board, period=PERIOD_LABELS[period], details=details), ephemeral=True
        )


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(ActivityCog(bot))
