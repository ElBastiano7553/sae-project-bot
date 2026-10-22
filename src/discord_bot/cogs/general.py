"""Commandes de consultation, ouvertes à tous les membres : /help, /status (/health), /project."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src import __version__
from src.config import ConfigError, load_link_catalog
from src.discord_bot.bot import SaeBot
from src.discord_bot.embeds import health_embed, help_embed, project_embed
from src.discord_bot.permissions import is_bot_admin
from src.gitlab.dispatcher import DEFAULT_BRANCH_KEY
from src.services.health import AISummary, AlertsSummary, HistorySummary, MessagesSummary, build_health_checks
from src.services.help import entries_by_category

log = logging.getLogger(__name__)

RECENT_EVENTS = 5


def build_status_embed(bot: SaeBot, *, title: str = "État du bot") -> discord.Embed:
    """Assemble /status à partir de mesures faites maintenant ; rien n'est supposé."""
    guild = bot.target_guild
    report = None
    try:
        report = bot.resolve_structure(guild) if guild else None
    except ConfigError:
        log.exception("Configuration invalide pendant /status")
    checks = build_health_checks(
        latency_ms=bot.latency * 1000,
        database_ok=bot.database.ping(),
        guild_found=guild is not None,
        structure=report,
        setup_confirmed=bot.setup_confirmed_at is not None,
        ai_provider=bot.settings.ai_provider,
        gitlab_enabled=bot.settings.gitlab_requested,
        gitlab_listening=bot.gitlab_server is not None and bot.gitlab_server.listening,
        gitlab_error=bot.gitlab_error,
        gitlab_last_event_at=bot.gitlab_events_repo.last_received_at(),
        database_schema=bot.database.schema_version,
        messages=_messages_summary(bot),
        alerts=AlertsSummary(
            channel_key=bot.settings.alerts_channel,
            channel_bound=bot.settings.alerts_channel in bot.bindings_repo.get_all() if bot.settings.alerts_channel else False,
            errors_24h=len(bot.bot_errors_repo.recent(_hours_ago(24), limit=1000)),
        ),
        history=_history_summary(bot),
        ai=AISummary(
            provider=bot.settings.ai_provider,
            model=bot.ai_provider.model if bot.ai_provider else None,
            config_error=bot.settings.ai_config_error,
            used_today=bot.assistant.today_usage() if bot.assistant else 0,
            daily_limit=bot.settings.ai_daily_limit,
            gitlab_api=bot.gitlab_api is not None,
        ),
    )
    uptime = dt.datetime.now(dt.UTC) - bot.started_at
    return health_embed(
        checks, uptime, __version__, last_error=bot.errors.last, error_count=bot.errors.total, title=title
    )


def _hours_ago(hours: int) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _history_summary(bot: SaeBot) -> HistorySummary:
    first, total = bot.gitlab_events_repo.coverage()
    return HistorySummary(
        retention_days=bot.settings.history_retention_days,
        events=total,
        first_event_at=first,
        deployments=len(bot.gitlab_events_repo.in_period(None, ("deployment",))),
    )


def _messages_summary(bot: SaeBot) -> MessagesSummary | None:
    try:
        statuses = bot.build_message_service().status(bot.bindings_repo.get_all())
    except ConfigError:
        log.exception("Contenu des messages permanents invalide pendant /status")
        return None
    return MessagesSummary(
        expected=len(statuses),
        published=sum(1 for s in statuses if s.published),
        errors=sum(1 for s in statuses if s.error),
        provisional=sum(1 for s in statuses if s.missing_links),
    )


class GeneralCog(commands.Cog):
    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="Aide : commandes disponibles, par catégorie, avec exemples")
    async def help(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        admin = isinstance(member, discord.Member) and is_bot_admin(member, self.bot.settings.admin_role_name)
        embed = help_embed(entries_by_category(include_admin=admin), show_admin_note=admin)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="État du bot et de ses intégrations (Discord, base, GitLab, IA)")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_status_embed(self.bot), ephemeral=True)

    @app_commands.command(name="health", description="Ancien nom de /status : état du bot et de ses intégrations")
    @app_commands.guild_only()
    async def health(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_status_embed(self.bot), ephemeral=True)

    @app_commands.command(name="project", description="Résumé du projet : liens, branche principale, derniers événements GitLab")
    @app_commands.guild_only()
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


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(GeneralCog(bot))
