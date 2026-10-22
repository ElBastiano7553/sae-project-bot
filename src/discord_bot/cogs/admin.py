"""Commandes slash d'administration : /setup, /refresh, /config, /health."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src import __version__
from src.config import ConfigError
from src.discord_bot.bot import SaeBot, install_error_handler
from src.discord_bot.embeds import config_embed, health_embed, structure_embed, sync_embed
from src.discord_bot.permissions import is_bot_admin
from src.discord_bot.views import ConfirmView
from src.services.health import build_health_checks
from src.services.permanent_messages import SyncAction

log = logging.getLogger(__name__)


def admin_only():
    """Vérification côté bot, en plus de la permission par défaut affichée par Discord."""

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: SaeBot = interaction.client  # type: ignore[assignment]
        member = interaction.user
        return isinstance(member, discord.Member) and is_bot_admin(member, bot.settings.admin_role_name)

    return app_commands.check(predicate)


class AdminCog(commands.Cog):
    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Prévisualiser puis confirmer la configuration du serveur")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @admin_only()
    async def setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = self._guild(interaction)
        report = self.bot.resolve_structure(guild)
        plan = await self.bot.build_message_service().plan(report.bindings)

        view = ConfirmView(interaction.user.id, self._apply_setup)
        view.original = interaction
        await interaction.followup.send(
            content=(
                "**Prévisualisation** : rien n'a encore été modifié.\n"
                "« Confirmer » enregistre les salons reconnus et publie ou met à jour les messages permanents. "
                "Aucun salon, rôle ou message d'utilisateur ne sera créé ni supprimé. (Expiration : 2 minutes.)"
            ),
            embeds=[structure_embed(report), sync_embed(plan, "📝 Messages permanents prévus")],
            view=view,
            ephemeral=True,
        )

    async def _apply_setup(self, interaction: discord.Interaction) -> None:
        try:
            guild = self._guild(interaction)
            report = self.bot.resolve_structure(guild)  # le serveur a pu changer pendant la confirmation
            self.bot.bindings_repo.replace_all(report.bindings)
            results = await self.bot.build_message_service().sync(report.bindings)
            self.bot.mark_setup_confirmed()
        except ConfigError as exc:
            await interaction.edit_original_response(content=f"⚠️ Problème de configuration :\n{exc}", embeds=[])
            return
        log.info("/setup confirmé par %s", interaction.user)
        await interaction.edit_original_response(
            content="✅ Configuration appliquée.",
            embeds=[structure_embed(report, "🔎 Salons enregistrés"), sync_embed(results, "📝 Messages permanents")],
        )

    @app_commands.command(name="refresh", description="Actualiser les messages permanents (recrée ceux qui ont été supprimés)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @admin_only()
    async def refresh(self, interaction: discord.Interaction) -> None:
        if not self.bot.setup_confirmed_at:
            await interaction.response.send_message(
                "Le serveur n'a pas encore été configuré : lancez d'abord `/setup` et confirmez.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = self.bot.resolve_structure(self._guild(interaction))
        results = await self.bot.build_message_service().sync(report.bindings)
        changed = sum(r.action is not SyncAction.UNCHANGED for r in results)
        await interaction.followup.send(
            content=f"Actualisation terminée : {changed} message(s) modifié(s) sur {len(results)}.",
            embed=sync_embed(results, "🔄 Messages permanents"),
            ephemeral=True,
        )

    @app_commands.command(name="config", description="Afficher l'état de la configuration du bot")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @admin_only()
    async def config(self, interaction: discord.Interaction) -> None:
        settings = self.bot.settings
        report = self.bot.resolve_structure(self._guild(interaction))
        settings_lines = [
            "Token Discord : défini ✅ (jamais affiché)",
            f"Serveur : `{settings.guild_id}`",
            f"Rôle administrateur : {settings.admin_role_name or '*non défini* (permission « Gérer le serveur »)'}",
            f"Base de données : `{settings.database_path.name}` (schéma v{self.bot.database.schema_version})",
            f"Actualisation au démarrage : {'oui' if settings.auto_refresh_on_startup else 'non'}",
            f"Épinglage des messages : {'oui' if settings.pin_permanent_messages else 'non'}",
            self._gitlab_config_line(),
            "IA : prévue en V4",
        ]
        await interaction.response.send_message(
            embed=config_embed(
                settings_lines=settings_lines,
                report=report,
                messages=self.bot.messages_repo.list_all(),
                guild_id=settings.guild_id,
                setup_confirmed_at=self.bot.setup_confirmed_at,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="health", description="Vérifier l'état du bot et de ses intégrations")
    @app_commands.guild_only()
    async def health(self, interaction: discord.Interaction) -> None:
        guild = self.bot.target_guild
        try:
            report = self.bot.resolve_structure(guild) if guild else None
        except ConfigError:
            log.exception("Configuration invalide pendant /health")
            report = None
        checks = build_health_checks(
            latency_ms=self.bot.latency * 1000,
            database_ok=self.bot.database.ping(),
            guild_found=guild is not None,
            structure=report,
            setup_confirmed=self.bot.setup_confirmed_at is not None,
            ai_provider=self.bot.settings.ai_provider,
            gitlab_enabled=self.bot.settings.gitlab_requested,
            gitlab_listening=self.bot.gitlab_server is not None and self.bot.gitlab_server.listening,
            gitlab_error=self.bot.gitlab_error,
            gitlab_last_event_at=self.bot.gitlab_events_repo.last_received_at(),
        )
        uptime = dt.datetime.now(dt.UTC) - self.bot.started_at
        await interaction.response.send_message(embed=health_embed(checks, uptime, __version__), ephemeral=True)

    def _gitlab_config_line(self) -> str:
        settings = self.bot.settings
        if settings.gitlab_config_error:
            return "Webhooks GitLab : configuration invalide, désactivés (détails dans /health)"
        if not settings.gitlab_webhooks_enabled:
            return "Webhooks GitLab : désactivés (GITLAB_WEBHOOK_SECRET vide)"
        project = f"projet `{settings.gitlab_project_id}`" if settings.gitlab_project_id else "tout projet (GITLAB_PROJECT_ID vide)"
        return (
            f"Webhooks GitLab : secret défini ✅ (jamais affiché), écoute sur "
            f"`{settings.webhook_host}:{settings.webhook_port}`, {project}"
        )

    def _guild(self, interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None or interaction.guild.id != self.bot.settings.guild_id:
            raise ConfigError("Cette commande doit être utilisée sur le serveur configuré dans DISCORD_GUILD_ID.")
        return interaction.guild


async def setup(bot: SaeBot) -> None:
    install_error_handler(bot.tree)
    await bot.add_cog(AdminCog(bot))
