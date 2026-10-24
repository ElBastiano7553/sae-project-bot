"""Groupe /projet : projets GitHub et GitLab suivis par Samae sur ce serveur (multi-projets, étape B1).

``liste`` et ``details`` sont ouvertes à tous les membres, en réponse éphémère :
un membre ne voit que les salons auxquels il a accès ; un administrateur voit
tout (salons manquants, auteur, emplacement de la configuration).

Chaque serveur ne voit que ses propres projets. Les projets configurés dans
``.env`` apparaissent comme « historiques », en lecture seule.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from src.config import ConfigError, load_server_layout
from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.embeds import project_detail_embed, projects_list_pages
from src.discord_bot.meeting_ui import PagesView
from src.discord_bot.permissions import is_bot_admin
from src.github.dispatcher import CHANNEL_KEY as GITHUB_CHANNEL_KEY
from src.gitlab.rules import load_gitlab_rules
from src.services.projects import ProjectView, legacy_projects, registered_view

REF_MAX = 20


class ProjectsCog(commands.Cog):
    projet = app_commands.Group(name="projet", description="Projets GitHub et GitLab suivis par Samae sur ce serveur", guild_only=True)

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    # --- Données ------------------------------------------------------------------------------

    def project_guilds(self) -> frozenset[int]:
        """Serveurs Discord où /projet est ouvert. Pour l'instant : DISCORD_GUILD_ID seulement."""
        return frozenset({self.bot.settings.guild_id})

    def _gitlab_routes(self) -> dict[str, str] | None:
        if not self.bot.settings.gitlab_requested:
            return None
        try:
            return dict(load_gitlab_rules(channel_keys=load_server_layout().channels.keys()).routes)
        except ConfigError:
            return None

    def projects_for(self, guild_id: int) -> list[ProjectView]:
        """Projets historiques (.env) puis projets enregistrés, uniquement ceux de ce serveur."""
        repo = self.bot.projects_repo
        bindings = self.bot.bindings_repo.get_all()
        registered = [registered_view(record, repo.routes(guild_id, record.id)) for record in repo.list_for_guild(guild_id)]
        legacy = legacy_projects(
            self.bot.settings,
            guild_id,
            channel_id_for=lambda key: self.bot.channel_id_for(key, bindings),
            gitlab_routes=self._gitlab_routes(),
            github_channel_key=GITHUB_CHANNEL_KEY,
        )
        return [*legacy, *registered]

    def find(self, guild_id: int, ref: str) -> ProjectView | None:
        ref = ref.strip()
        return next((p for p in self.projects_for(guild_id) if p.ref == ref), None)

    async def _guild_or_error(self, interaction: discord.Interaction) -> discord.Guild | None:
        guild = interaction.guild
        if guild is None or guild.id not in self.project_guilds():
            await send_error(interaction, "Le suivi des projets n'est pas encore ouvert sur ce serveur.", title="Serveur non pris en charge")
            return None
        return guild

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        return getattr(member, "guild_permissions", None) is not None and is_bot_admin(member, self.bot.settings.admin_role_name)

    # --- Commandes ----------------------------------------------------------------------------

    @projet.command(name="liste", description="Projets GitHub et GitLab suivis sur ce serveur")
    async def list_projects(self, interaction: discord.Interaction) -> None:
        guild = await self._guild_or_error(interaction)
        if guild is None:
            return
        pages = projects_list_pages(self.projects_for(guild.id))
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
            return
        # Plus de PROJECTS_PER_PAGE projets : boutons « Précédente » / « Suivante », réservés à l'auteur.
        view = PagesView(pages, interaction.user.id)
        view.original = interaction
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

    @projet.command(name="details", description="Détails d'un projet suivi : dépôt, état, salons des notifications")
    @app_commands.describe(projet="Projet (référence affichée par /projet liste)")
    async def details(self, interaction: discord.Interaction, projet: app_commands.Range[str, 1, REF_MAX]) -> None:
        guild = await self._guild_or_error(interaction)
        if guild is None:
            return
        project = self.find(guild.id, projet)
        if project is None:
            await send_error(interaction, "Projet introuvable sur ce serveur. Les références sont affichées par `/projet liste`.", title="Projet introuvable")
            return
        member = interaction.user

        def can_view(channel_id: int) -> bool:
            channel = guild.get_channel(channel_id)
            return channel is not None and channel.permissions_for(member).view_channel

        embed = project_detail_embed(project, can_view=can_view, admin=self._is_admin(interaction))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @details.autocomplete("projet")
    async def _project_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None or guild.id not in self.project_guilds():
            return []
        needle = current.casefold()
        choices = [
            app_commands.Choice(name=f"{p.name} ({p.provider_label})"[:100], value=p.ref)
            for p in self.projects_for(guild.id)
            if needle in p.name.casefold() or needle in p.ref
        ]
        return choices[:25]


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(ProjectsCog(bot))
