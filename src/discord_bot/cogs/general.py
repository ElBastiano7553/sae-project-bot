"""Commandes de consultation, ouvertes à tous les membres : /aide et /etat."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src import __version__
from src.config import ConfigError
from src.discord_bot.bot import SaeBot
from src.discord_bot.embeds import health_embed
from src.discord_bot.help_ui import HelpView, collect_commands, render_pages
from src.discord_bot.permissions import is_bot_admin
from src.services.backup import list_backups
from src.services.health import (
    AISummary,
    AlertsSummary,
    BackupSummary,
    GitHubSummary,
    ProjectsReceptionSummary,
    HistorySummary,
    MessagesSummary,
    build_health_checks,
)
from src.services.help import GUIDE_PAGES

log = logging.getLogger(__name__)

HELP_SECTIONS = [app_commands.Choice(name=page.title, value=page.key) for page in GUIDE_PAGES]


def build_status_embed(bot: SaeBot, *, title: str = "État du bot") -> discord.Embed:
    """Assemble /etat à partir de mesures faites maintenant ; rien n'est supposé."""
    guild = bot.target_guild
    report = None
    try:
        report = bot.resolve_structure(guild) if guild else None
    except ConfigError:
        log.exception("Configuration invalide pendant /etat")
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
            errors_24h=bot.bot_errors_repo.count_since(_hours_ago(24)),
        ),
        history=_history_summary(bot),
        backups=None if bot.database.in_memory else _backup_summary(bot),
        ai=AISummary(
            provider=bot.settings.ai_provider,
            model=bot.ai_provider.model if bot.ai_provider else None,
            config_error=bot.settings.ai_config_error,
            used_today=bot.assistant.today_usage() if bot.assistant else 0,
            daily_limit=bot.settings.ai_daily_limit,
            gitlab_api=bot.gitlab_api is not None,
        ),
        interactions=bot.interaction_stats.summary(),
        github=GitHubSummary(
            enabled=bot.settings.github_requested,
            listening=bot.github_server is not None and bot.github_server.listening,
            error=bot.github_error,
            last_event_at=bot.github_deliveries_repo.last_received_at(),
            repository=bot.settings.github_repository,
        ),
        projects=ProjectsReceptionSummary(
            enabled=bot.settings.projects_http_enabled,
            listening=bot.projects_server is not None and bot.projects_server.listening,
            error=bot.projects_error,
            address=bot.projects_server.local_url if bot.projects_server is not None else "",
            github_app=bool(bot.settings.github_app_webhook_secret),
            last_event_at=bot.project_deliveries_repo.last_received_at(),
            github_connect=bot.github_setup is not None,
            github_connect_error=bot.settings.github_app_error,
            gitlab_connect=bot.gitlab_setup is not None,
            gitlab_connect_error=bot.settings.gitlab_app_error,
        ),
    )
    uptime = dt.datetime.now(dt.UTC) - bot.started_at
    return health_embed(
        checks, uptime, __version__, last_error=bot.errors.last, error_count=bot.errors.total, title=title
    )


def _hours_ago(hours: int) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _backup_summary(bot: SaeBot) -> BackupSummary:
    backups = list_backups(bot.settings.backup_dir) if bot.settings.backup_dir else []
    age = (dt.datetime.now(dt.UTC) - backups[0].created_at).total_seconds() / 3600 if backups else None
    return BackupSummary(bot.settings.backup_interval_hours, age, len(backups))


def _history_summary(bot: SaeBot) -> HistorySummary:
    first, total = bot.gitlab_events_repo.coverage()
    return HistorySummary(
        retention_days=bot.settings.history_retention_days,
        events=total,
        first_event_at=first,
        deployments=bot.gitlab_events_repo.count(("deployment",)),
    )


def _messages_summary(bot: SaeBot) -> MessagesSummary | None:
    try:
        statuses = bot.build_message_service().status(bot.bindings_repo.get_all())
    except ConfigError:
        log.exception("Contenu des messages permanents invalide pendant /etat")
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

    @app_commands.command(name="aide", description="Centre d'aide : présentation, réunions, commandes, organisation, dépannage")
    @app_commands.describe(section="Ouvrir directement une section (Accueil par défaut)")
    @app_commands.choices(section=HELP_SECTIONS)
    async def help(self, interaction: discord.Interaction, section: app_commands.Choice[str] | None = None) -> None:
        member = interaction.user
        # Les pages d'administration détaillées ne sont montrées qu'aux administrateurs du bot.
        admin = isinstance(member, discord.Member) and is_bot_admin(member, self.bot.settings.admin_role_name)
        pages = render_pages(GUIDE_PAGES, commands=collect_commands(self.bot.tree), admin=admin, heading="Aide de Samae")
        view = HelpView(pages, interaction.user.id)
        if section is not None:
            view.go_to_section(section.value)
        view.original = interaction
        await interaction.response.send_message(embed=view.embed, view=view, ephemeral=True)

    @app_commands.command(name="etat", description="État du bot et de ses intégrations (Discord, base, GitLab, GitHub, IA)")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_status_embed(self.bot), ephemeral=True)


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(GeneralCog(bot))
