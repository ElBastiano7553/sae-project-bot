"""Groupe /projet : projets GitHub et GitLab suivis par Samae sur ce serveur (multi-projets, étapes B1 et B2).

``liste`` et ``details`` sont ouvertes à tous les membres, en réponse éphémère :
un membre ne voit que les salons auxquels il a accès ; un administrateur voit
tout (salons manquants, auteur, emplacement de la configuration).

Administrateurs seulement (vérifié par le bot à chaque appel) : ``ajouter`` enregistre
un projet à partir de l'adresse de son dépôt, après un récapitulatif et une confirmation ;
``modifier`` choisit ensuite le salon et les filtres de chaque catégorie de notifications.
Les deux opérations restent distinctes.

Chaque serveur ne voit que ses propres projets. Les projets configurés dans
``.env`` apparaissent comme « historiques », en lecture seule.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from src.config import ConfigError, load_server_layout
from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.cogs.admin import admin_only
from src.discord_bot.embeds import (
    project_add_preview_embed,
    project_added_embed,
    project_detail_embed,
    project_route_embed,
    projects_list_pages,
)
from src.discord_bot.meeting_ui import PagesView
from src.discord_bot.views import ConfirmView
from src.discord_bot.permissions import is_bot_admin
from src.github.dispatcher import CHANNEL_KEY as GITHUB_CHANNEL_KEY
from src.gitlab.rules import load_gitlab_rules
from src.services.projects import (
    CATEGORIES,
    CLEAR_FILTER,
    NAME_MAX,
    URL_MAX,
    ParsedRepository,
    ProjectError,
    ProjectView,
    Route,
    legacy_projects,
    normalize_name,
    parse_repository_url,
    registered_view,
    update_filters,
)

REF_MAX = 20
FILTER_TEXT_MAX = 300
CATEGORY_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in CATEGORIES.items()]
# Permissions dont Samae a besoin dans un salon de notifications.
NOTIFY_PERMISSIONS = {"view_channel": "Voir le salon", "send_messages": "Envoyer des messages", "embed_links": "Intégrer des liens"}


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

    @projet.command(name="ajouter", description="Admins : enregistrer un projet GitHub ou GitLab à partir de son adresse", extras={"admin": True})
    @app_commands.describe(
        url="Adresse du dépôt, par exemple https://github.com/equipe/jeu ou https://gitlab.com/groupe/projet",
        nom="Nom affiché dans Samae (par défaut : le nom du dépôt)",
    )
    @admin_only()
    async def add(
        self,
        interaction: discord.Interaction,
        url: app_commands.Range[str, 1, URL_MAX],
        nom: app_commands.Range[str, 1, NAME_MAX] | None = None,
    ) -> None:
        guild = await self._guild_or_error(interaction)
        if guild is None:
            return
        try:
            parsed = parse_repository_url(url, gitlab_hosts=self._gitlab_hosts())
            name = normalize_name(nom or parsed.default_name)
        except ProjectError as exc:
            await send_error(interaction, str(exc), title="Adresse refusée", tone="warning")
            return
        duplicate = self._duplicate(guild.id, parsed)
        if duplicate:
            await send_error(interaction, duplicate, title="Projet déjà suivi", tone="warning")
            return

        async def confirm(confirmation: discord.Interaction) -> None:
            # Revérifié : un autre administrateur a pu ajouter le même projet pendant la confirmation.
            again = self._duplicate(guild.id, parsed)
            if again:
                await confirmation.edit_original_response(content=again, embeds=[], view=None)
                return
            record = self.bot.projects_repo.create(
                guild.id, parsed.provider, parsed.external_id, name, created_by=interaction.user.id, instance_url=parsed.instance_url
            )
            self.bot.record_admin_action(interaction, "projet ajouter", str(record.id), f"{parsed.provider} {parsed.external_id}")
            view = registered_view(record, ())
            await confirmation.edit_original_response(content=None, embed=project_added_embed(view), view=None)

        view = ConfirmView(interaction.user.id, confirm, command="/projet ajouter")
        view.original = interaction
        embed = project_add_preview_embed(parsed, name, guild.name, warning=self._legacy_warning(parsed))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _gitlab_hosts(self) -> tuple[str, ...]:
        host = self.bot.settings.gitlab_base_url.removeprefix("https://").split("/")[0]
        return (host,) if host else ()

    def _duplicate(self, guild_id: int, parsed: ParsedRepository) -> str:
        """Message d'erreur si ce serveur suit déjà ce projet (en base ou dans .env), sinon vide."""
        existing = self.bot.projects_repo.find(guild_id, parsed.provider, parsed.external_id, parsed.instance_url)
        if existing is not None:
            return f"Ce projet est déjà suivi sur ce serveur sous le nom « {existing.name} » (référence `{existing.id}`). Voir `/projet details`."
        settings = self.bot.settings
        if (
            parsed.provider == "github"
            and guild_id == settings.guild_id
            and settings.github_requested
            and settings.github_repository.casefold() == parsed.external_id
        ):
            return (
                "Ce dépôt est déjà suivi par la configuration `.env` (projet historique `env-github`). "
                "L'ajouter une seconde fois doublerait ses notifications : rien n'a été enregistré."
            )
        return ""

    def _legacy_warning(self, parsed: ParsedRepository) -> str:
        settings = self.bot.settings
        if parsed.provider == "gitlab" and settings.gitlab_requested and parsed.instance_url == settings.gitlab_base_url:
            return (
                "Un projet GitLab est déjà suivi par la configuration `.env` (`env-gitlab`, identifié par son numéro). "
                "Vérifiez qu'il ne s'agit pas du même projet : sinon, ses notifications seraient doublées plus tard."
            )
        return ""

    @projet.command(
        name="modifier",
        description="Admins : salon et filtres d'une catégorie de notifications d'un projet",
        extras={"admin": True},
    )
    @app_commands.describe(
        projet="Projet enregistré (référence affichée par /projet liste)",
        categorie="Catégorie de notifications",
        salon="Salon où publier cette catégorie",
        branches=f"Branches suivies, séparées par des virgules (jokers acceptés) ; « {CLEAR_FILTER} » pour tout suivre",
        filtre=f"Actions ou statuts notifiés, séparés par des virgules ; « {CLEAR_FILTER} » pour tout notifier",
        retirer="Ne plus publier cette catégorie (supprime son salon et ses filtres)",
    )
    @app_commands.choices(categorie=CATEGORY_CHOICES)
    @admin_only()
    async def modify(
        self,
        interaction: discord.Interaction,
        projet: app_commands.Range[str, 1, REF_MAX],
        categorie: app_commands.Choice[str],
        salon: discord.TextChannel | None = None,
        branches: app_commands.Range[str, 1, FILTER_TEXT_MAX] | None = None,
        filtre: app_commands.Range[str, 1, FILTER_TEXT_MAX] | None = None,
        retirer: bool = False,
    ) -> None:
        guild = await self._guild_or_error(interaction)
        if guild is None:
            return
        project = self.find(guild.id, projet)
        if project is None:
            await send_error(interaction, "Projet introuvable sur ce serveur. Les références sont affichées par `/projet liste`.", title="Projet introuvable")
            return
        if project.legacy:
            await send_error(
                interaction,
                "Ce projet est configuré dans `.env` (et `config/gitlab.toml` pour GitLab) : il se modifie sur le serveur "
                "qui héberge Samae, puis redémarrage. Rien n'a été changé.",
                title="Projet historique",
                tone="warning",
            )
            return
        category = categorie.value
        project_id = int(project.ref)
        current = next((r for r in project.routes if r.category == category), None)
        repo = self.bot.projects_repo

        if retirer:
            if salon is not None or branches is not None or filtre is not None:
                await send_error(interaction, "« retirer » s'utilise seul : sans salon, branches ni filtre.", tone="warning")
                return
            if current is None:
                await send_error(interaction, f"La catégorie « {categorie.name} » n'est pas publiée pour ce projet : rien à retirer.", tone="warning")
                return
            repo.remove_route(guild.id, project_id, category)
            self.bot.record_admin_action(interaction, "projet modifier", f"{project.ref}:{category}", "retirée")
            await interaction.response.send_message(embed=project_route_embed(project, category, None, removed=True), ephemeral=True)
            return

        if salon is None and branches is None and filtre is None:
            await send_error(interaction, "Indiquez au moins un salon, des branches, un filtre, ou « retirer ».", tone="warning")
            return
        channel_id = current.channel_id if current is not None else None
        if salon is not None:
            problem = self._channel_problem(guild, salon.id)
            if problem:
                await send_error(interaction, problem, title="Salon refusé")
                return
            channel_id = salon.id
        if channel_id is None:
            await send_error(interaction, f"Choisissez d'abord le salon de la catégorie « {categorie.name} » (option « salon »).", tone="warning")
            return
        try:
            filters = update_filters(category, current.filters if current else {}, branches=branches, values=filtre)
        except ProjectError as exc:
            await send_error(interaction, str(exc), title="Filtre refusé", tone="warning")
            return
        repo.set_route(guild.id, project_id, category, channel_id, filters)
        self.bot.record_admin_action(interaction, "projet modifier", f"{project.ref}:{category}", f"salon {channel_id}")
        embed = project_route_embed(project, category, Route(category, channel_id, filters), previous=current)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _channel_problem(self, guild: discord.Guild, channel_id: int) -> str:
        """Le salon doit appartenir à CE serveur, être textuel, et Samae doit pouvoir y publier."""
        channel = guild.get_channel(channel_id)
        if channel is None or getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            return "Ce salon n'appartient pas à ce serveur (ou Samae ne le voit pas)."
        if channel.type not in (discord.ChannelType.text, discord.ChannelType.news):
            return "Choisissez un salon textuel ou d'annonces."
        perms = channel.permissions_for(guild.me)
        missing = [label for name, label in NOTIFY_PERMISSIONS.items() if not getattr(perms, name)]
        if missing:
            return f"Samae ne peut pas publier dans {channel.mention if hasattr(channel, 'mention') else 'ce salon'} : il lui manque " + ", ".join(
                f"« {m} »" for m in missing
            ) + "."
        return ""

    @modify.autocomplete("projet")
    async def _registered_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = await self._project_autocomplete(interaction, current)
        return [c for c in choices if c.value.isdigit()]  # les projets historiques ne se modifient pas ici

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
