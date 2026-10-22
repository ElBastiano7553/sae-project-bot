"""Commandes d'administration : /setup, /refresh, /config, /announce et le groupe /admin.

Chaque commande est protégée deux fois : la permission par défaut « Gérer le serveur »
masque la commande dans Discord, et ``admin_only`` revérifie côté bot à chaque appel
(un administrateur du serveur peut modifier la visibilité des commandes).
"""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.config import ConfigError, load_link_catalog, load_server_layout
from src.discord_bot.bot import SaeBot, install_error_handler
from src.discord_bot.cogs.general import build_status_embed
from src.discord_bot.embeds import (
    config_embed,
    diagnostic_embed,
    history_embed,
    make_embed,
    messages_status_embed,
    payload_to_embed,
    structure_embed,
    sync_embed,
)
from src.discord_bot.permissions import is_bot_admin
from src.discord_bot.views import AnnouncementModal, ConfirmView
from src.gitlab.rules import load_gitlab_rules
from src.services.content import ANNOUNCEMENT_BODY_MAX, ANNOUNCEMENT_TITLE_MAX, Announcement, load_message_templates
from src.services.design import Tone
from src.services.diagnostics import ChannelDiagnostic, channel_uses, missing_permissions, required_permissions
from src.services.permanent_messages import SyncAction

log = logging.getLogger(__name__)

ANNOUNCEMENT_MESSAGE_KEY = "annonce-en-cours"
HISTORY_LIMIT = 15
CHECKED_PERMISSIONS = (
    "view_channel", "send_messages", "embed_links", "read_message_history", "pin_messages", "manage_messages",
)


def admin_only():
    """Vérification côté bot, en plus de la permission par défaut affichée par Discord."""

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: SaeBot = interaction.client  # type: ignore[assignment]
        member = interaction.user
        return isinstance(member, discord.Member) and is_bot_admin(member, bot.settings.admin_role_name)

    return app_commands.check(predicate)


async def message_key_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        keys = [t.key for t in load_message_templates()]
    except ConfigError:
        return []
    current = current.casefold()
    return [app_commands.Choice(name=k, value=k) for k in keys if current in k.casefold()][:25]


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
        self.bot.record_admin_action(interaction, "setup", "", f"{len(report.bindings)} salons associés")
        await interaction.edit_original_response(
            content="✅ Configuration appliquée.",
            embeds=[structure_embed(report, "🔎 Salons enregistrés"), sync_embed(results, "📝 Messages permanents")],
        )

    @app_commands.command(name="refresh", description="Mettre à jour les messages permanents sur place (recrée ceux qui ont été supprimés)")
    @app_commands.describe(message="Un seul message à actualiser (laisser vide pour tous)")
    @app_commands.autocomplete(message=message_key_autocomplete)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @admin_only()
    async def refresh(self, interaction: discord.Interaction, message: str | None = None) -> None:
        if not self.bot.setup_confirmed_at:
            await interaction.response.send_message(
                "Le serveur n'a pas encore été configuré : lancez d'abord `/setup` et confirmez.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = self.bot.resolve_structure(self._guild(interaction))
        keys = [message] if message else None
        results = await self.bot.build_message_service().sync(report.bindings, keys=keys)
        changed = sum(r.action is not SyncAction.UNCHANGED for r in results)
        self.bot.record_admin_action(interaction, "refresh", message or "tous", f"{changed} modifié(s) sur {len(results)}")
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
            f"Recréation auto. des messages supprimés : {'oui' if settings.auto_recreate_deleted else 'non'}",
            self._gitlab_config_line(),
            f"Historique conservé : {settings.history_retention_days} jours",
            f"Alertes d'erreurs : salon `{settings.alerts_channel}`" if settings.alerts_channel else "Alertes d'erreurs : désactivées",
            "IA : non intégrée dans cette version",
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

    # --- Annonce en cours ---------------------------------------------------------

    @app_commands.command(name="announce", description="Rédiger ou modifier l'annonce en cours de #annonces")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @admin_only()
    async def announce(self, interaction: discord.Interaction) -> None:
        if not self.bot.setup_confirmed_at:
            await interaction.response.send_message(
                "Le serveur n'a pas encore été configuré : lancez d'abord `/setup` et confirmez.", ephemeral=True
            )
            return
        current = self.bot.announcement
        modal = AnnouncementModal(
            title_default=current.title if current else "",
            body_default=current.body if current else "",
            title_max=ANNOUNCEMENT_TITLE_MAX,
            body_max=ANNOUNCEMENT_BODY_MAX,
            on_submit=self._preview_announcement,
        )
        await interaction.response.send_modal(modal)

    async def _preview_announcement(self, interaction: discord.Interaction, title: str, body: str) -> None:
        announcement = Announcement(
            title=title.strip(),
            body=body.strip(),
            author=getattr(interaction.user, "display_name", str(interaction.user)),
            updated_at=dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        )
        payload = self.bot.render_permanent(ANNOUNCEMENT_MESSAGE_KEY, announcement=announcement)

        async def confirm(confirm_interaction: discord.Interaction) -> None:
            self.bot.save_announcement(announcement)
            report = self.bot.resolve_structure(self._guild(confirm_interaction))
            results = await self.bot.build_message_service().sync(report.bindings, keys=[ANNOUNCEMENT_MESSAGE_KEY])
            self.bot.record_admin_action(confirm_interaction, "announce", announcement.title[:100], results[0].action)
            await confirm_interaction.edit_original_response(
                content="✅ Annonce enregistrée.", embeds=[sync_embed(results, "📣 Annonce en cours")]
            )

        view = ConfirmView(interaction.user.id, confirm, command="/announce")
        view.original = interaction
        await interaction.response.send_message(
            content=(
                "**Prévisualisation** de l'annonce, rien n'est encore publié. « Confirmer » modifie le message "
                "d'annonce existant de #annonces (ou le publie s'il n'existe pas encore). Expiration : 2 minutes."
            ),
            embed=payload_to_embed(payload),
            view=view,
            ephemeral=True,
        )

    # --- Groupe /admin ----------------------------------------------------------------

    admin = app_commands.Group(
        name="admin",
        description="Outils d'administration du bot",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @admin.command(name="messages", description="État de chaque message permanent (lien, dernière action, erreurs)")
    @admin_only()
    async def admin_messages(self, interaction: discord.Interaction) -> None:
        statuses = self.bot.build_message_service().status(self.bot.bindings_repo.get_all())
        embed = messages_status_embed(statuses, self.bot.settings.guild_id, load_link_catalog())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="rebuild", description="Reconstruire un message permanent (nouveau message, ancien supprimé)")
    @app_commands.describe(message="Message permanent à reconstruire")
    @app_commands.autocomplete(message=message_key_autocomplete)
    @admin_only()
    async def admin_rebuild(self, interaction: discord.Interaction, message: str) -> None:
        if not self.bot.setup_confirmed_at:
            await interaction.response.send_message(
                "Le serveur n'a pas encore été configuré : lancez d'abord `/setup` et confirmez.", ephemeral=True
            )
            return
        report = self.bot.resolve_structure(self._guild(interaction))
        preview = await self.bot.build_message_service().rebuild(message, report.bindings, apply=False)
        if preview.action is not SyncAction.REBUILD:
            await interaction.response.send_message(embed=sync_embed([preview], "🧱 Reconstruction impossible"), ephemeral=True)
            return

        async def confirm(confirm_interaction: discord.Interaction) -> None:
            fresh = self.bot.resolve_structure(self._guild(confirm_interaction))
            result = await self.bot.build_message_service().rebuild(message, fresh.bindings)
            self.bot.record_admin_action(confirm_interaction, "rebuild", message, result.action)
            await confirm_interaction.edit_original_response(content=None, embeds=[sync_embed([result], "🧱 Reconstruction")])

        view = ConfirmView(interaction.user.id, confirm, command="/admin rebuild")
        view.original = interaction
        warning = make_embed(
            f"🧱 Reconstruire « {message} » ?",
            "Un **nouveau** message sera publié, puis l'ancien message du bot sera **supprimé** : "
            "son lien Discord et ses réactions seront perdus.\n\n"
            "Pour une simple mise à jour du texte, utilisez plutôt `/refresh`, qui modifie le message sur place.",
            tone=Tone.WARNING,
        )
        await interaction.response.send_message(embed=warning, view=view, ephemeral=True)

    @admin.command(name="diagnostic", description="Vérifier les permissions du bot dans chaque salon utilisé")
    @admin_only()
    async def admin_diagnostic(self, interaction: discord.Interaction) -> None:
        guild = self._guild(interaction)
        report = self.bot.resolve_structure(guild)
        results = self._diagnose_channels(guild, report.bindings)
        status = build_status_embed(self.bot, title="État des intégrations")
        await interaction.response.send_message(embeds=[diagnostic_embed(results), status], ephemeral=True)

    @admin.command(name="history", description="Dernières actions d'administration (qui, quoi, quand)")
    @admin_only()
    async def admin_history(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=history_embed(self.bot.admin_log.recent(HISTORY_LIMIT)), ephemeral=True)

    def _diagnose_channels(self, guild: discord.Guild, bindings: dict[str, int]) -> list[ChannelDiagnostic]:
        message_channels = [t.channel_key for t in load_message_templates()]
        notification_channels: list[str] = []
        if self.bot.settings.gitlab_webhooks_enabled:
            try:
                rules = load_gitlab_rules(channel_keys=load_server_layout().channels.keys())
                notification_channels = list(rules.routes.values())
            except ConfigError as exc:
                log.warning("Diagnostic : config/gitlab.toml invalide (%s)", exc)
        me = guild.me
        results = []
        for key, uses in channel_uses(message_channels, notification_channels).items():
            channel_id = bindings.get(key)
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None or me is None:
                results.append(ChannelDiagnostic(key, channel_id, uses, problem="salon non associé : lancez /setup"))
                continue
            perms = channel.permissions_for(me)
            granted = {name: bool(getattr(perms, name, False)) for name in CHECKED_PERMISSIONS}
            required = required_permissions(uses, self.bot.settings.pin_permanent_messages)
            results.append(ChannelDiagnostic(key, channel_id, uses, tuple(missing_permissions(granted, required))))
        return results

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
