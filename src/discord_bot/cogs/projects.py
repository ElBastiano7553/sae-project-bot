"""Groupe /projet : projets GitHub et GitLab suivis par Samae sur ce serveur (multi-projets, étapes B1 et B2).

``liste`` et ``details`` sont ouvertes à tous les membres, en réponse éphémère :
un membre ne voit que les salons auxquels il a accès, et un projet dont il ne voit
aucun salon lui est entièrement masqué (liste, détails, autocomplétion : « introuvable »,
comme un projet qui n'existe pas). Un administrateur voit tout (salons manquants,
auteur, emplacement de la configuration).

Administrateurs seulement (vérifié par le bot à chaque appel) : ``ajouter`` enregistre
un projet à partir de l'adresse de son dépôt, après un récapitulatif et une confirmation ;
``modifier`` choisit ensuite le salon et les filtres de chaque catégorie de notifications ;
``configurer`` relie un dépôt GitHub à la GitHub App de Samae (étape B4) : lien d'installation
personnel et à usage unique, vérification auprès de GitHub, choix du dépôt, confirmation,
message de test. Aucun token ni mot de passe n'est demandé dans Discord ;
``diagnostic`` explique pourquoi un projet ne reçoit pas ses notifications
(``services/project_diagnosis.py``), sans rien modifier.

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
    github_connect_embed,
    github_connect_preview_embed,
    github_connected_embed,
    project_test_embed,
    project_add_preview_embed,
    project_added_embed,
    project_detail_embed,
    project_diagnosis_embed,
    project_route_embed,
    projects_list_pages,
)
from src.discord_bot.meeting_ui import PagesView
from src.discord_bot.project_views import MAX_OPTIONS, GithubConnectView, RepositorySelectView, TestMessageView
from src.discord_bot.views import ConfirmView
from src.discord_bot.permissions import is_bot_admin
from src.github.dispatcher import CHANNEL_KEY as GITHUB_CHANNEL_KEY
from src.gitlab.rules import load_gitlab_rules
from src.providers.github_app import STATE_TTL
from src.services.project_diagnosis import DIAGNOSIS_DAYS, DeliveryFacts, ProjectFacts, ReceptionFacts, diagnose
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
    project_visible,
    registered_view,
    update_filters,
)

REF_MAX = 20
REPOSITORY_MAX = 140
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
        linked = repo.linked_ids(guild_id)
        registered = [
            registered_view(record, repo.routes(guild_id, record.id), connected=record.id in linked) for record in repo.list_for_guild(guild_id)
        ]
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

    def visible_projects(self, guild: discord.Guild, member) -> list[ProjectView]:
        """Projets que ce membre peut voir : tous pour un administrateur, sinon ceux dont il voit au moins un salon."""
        projects = self.projects_for(guild.id)
        if self._member_is_admin(member):
            return projects
        can_view = self._can_view(guild, member)
        return [p for p in projects if project_visible(p, can_view)]

    @staticmethod
    def _can_view(guild: discord.Guild, member):
        def can_view(channel_id: int) -> bool:
            channel = guild.get_channel(channel_id)
            return channel is not None and channel.permissions_for(member).view_channel

        return can_view

    async def _guild_or_error(self, interaction: discord.Interaction) -> discord.Guild | None:
        guild = interaction.guild
        if guild is None or guild.id not in self.project_guilds():
            await send_error(interaction, "Le suivi des projets n'est pas encore ouvert sur ce serveur.", title="Serveur non pris en charge")
            return None
        return guild

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return self._member_is_admin(interaction.user)

    def _member_is_admin(self, member) -> bool:
        return getattr(member, "guild_permissions", None) is not None and is_bot_admin(member, self.bot.settings.admin_role_name)

    # --- Commandes ----------------------------------------------------------------------------

    @projet.command(name="liste", description="Projets GitHub et GitLab suivis sur ce serveur")
    async def list_projects(self, interaction: discord.Interaction) -> None:
        guild = await self._guild_or_error(interaction)
        if guild is None:
            return
        pages = projects_list_pages(self.visible_projects(guild, interaction.user), admin=self._is_admin(interaction))
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
        ref = projet.strip()
        # Un projet masqué reçoit exactement la même réponse qu'une référence inconnue.
        project = next((p for p in self.visible_projects(guild, interaction.user) if p.ref == ref), None)
        if project is None:
            await send_error(interaction, "Projet introuvable sur ce serveur. Les références sont affichées par `/projet liste`.", title="Projet introuvable")
            return
        can_view = self._can_view(guild, interaction.user)
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

    @projet.command(
        name="configurer",
        description="Admins : connecter un dépôt GitHub à Samae (GitHub App, sans aucun token dans Discord)",
        extras={"admin": True},
    )
    @app_commands.describe(depot="Dépôt déjà autorisé à connecter directement (sinon : lien d'installation, puis menu)")
    @admin_only()
    async def configure(self, interaction: discord.Interaction, depot: app_commands.Range[str, 1, REPOSITORY_MAX] | None = None) -> None:
        guild = await self._guild_or_error(interaction)
        if guild is None:
            return
        problem = self._github_connection_unavailable()
        if problem:
            await send_error(interaction, problem, title="Connexion GitHub indisponible", tone="warning")
            return
        if depot is not None:
            await self._preview_connection(interaction, depot.strip())
            return
        flow = self.bot.github_setup
        install_url = flow.install_url(guild.id, interaction.user.id)
        installations = self.bot.github_setup_repo.installations(guild.id)
        view = GithubConnectView(interaction.user.id, install_url, self._choose_repository)
        view.original = interaction
        embed = github_connect_embed(installations, expires_minutes=int(STATE_TTL.total_seconds() // 60))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _github_connection_unavailable(self) -> str:
        settings = self.bot.settings
        if self.bot.github_setup is None:
            reason = f" Configuration refusée : {settings.github_app_error}" if settings.github_app_error else ""
            return (
                "La connexion GitHub n'est pas encore activée par l'hébergeur de Samae (GitHub App, adresse HTTPS : "
                f"voir `docs/github-app.md`).{reason} En attendant, `/projet ajouter` enregistre le projet sans le connecter."
            )
        server = self.bot.projects_server
        if server is None or not server.listening:
            return "La réception des événements des projets est arrêtée : voir `/etat`. Rien n'a été relié."
        return ""

    async def _admin_still(self, interaction: discord.Interaction) -> discord.Guild | None:
        """Revérifie, à chaque bouton, que la personne est toujours administratrice de CE serveur."""
        guild = interaction.guild
        if guild is None or guild.id not in self.project_guilds() or not self._is_admin(interaction):
            await send_error(interaction, "Réservé aux administrateurs de ce serveur.", title="Accès refusé")
            return None
        return guild

    def _github_candidates(self, guild_id: int) -> list[str]:
        """Dépôts des installations vérifiées pour CE serveur qui ne sont pas encore connectés ici."""
        linked = self.bot.projects_repo.linked_ids(guild_id)
        connected = {p.external_id for p in self.bot.projects_repo.list_for_guild(guild_id) if p.provider == "github" and p.id in linked}
        seen: dict[str, str] = {}
        for installation in self.bot.github_setup_repo.installations(guild_id):
            for name in installation.repositories:
                if name.casefold() not in connected:
                    seen.setdefault(name.casefold(), name)
        return sorted(seen.values(), key=str.casefold)

    async def _choose_repository(self, interaction: discord.Interaction) -> None:
        guild = await self._admin_still(interaction)
        if guild is None:
            return
        candidates = self._github_candidates(guild.id)
        if not candidates:
            await send_error(
                interaction,
                "Aucun dépôt à connecter pour l'instant : terminez l'installation sur GitHub (avec l'autorisation demandée "
                "ensuite), puis cliquez à nouveau. Les dépôts déjà connectés ne sont pas proposés.",
                title="Aucun dépôt disponible",
                tone="warning",
            )
            return
        note = ""
        if len(candidates) > MAX_OPTIONS:
            note = f" Seuls les {MAX_OPTIONS} premiers sont proposés : pour un autre, utilisez `/projet configurer depot:`."
        view = RepositorySelectView(interaction.user.id, candidates, self._preview_connection)
        view.original = interaction
        await interaction.response.send_message(f"Choisissez le dépôt à connecter.{note}", view=view, ephemeral=True)

    def _connection_problem(self, guild_id: int, repository: str) -> str:
        installation = self.bot.github_setup_repo.installation_for(guild_id, repository)
        if installation is None:
            return (
                "Ce dépôt n'est couvert par aucune installation de la GitHub App vérifiée pour ce serveur. "
                "Lancez `/projet configurer` sans option et suivez le lien d'installation."
            )
        external_id = repository.casefold()
        existing = self.bot.projects_repo.find(guild_id, "github", external_id)
        if existing is not None and existing.id in self.bot.projects_repo.linked_ids(guild_id):
            return f"Ce dépôt est déjà connecté sur ce serveur (projet `{existing.id}`). Voir `/projet details`."
        settings = self.bot.settings
        if guild_id == settings.guild_id and settings.github_requested and settings.github_repository.casefold() == external_id:
            return (
                "Ce dépôt est déjà suivi par la configuration `.env` (projet historique `env-github`). Le connecter aussi "
                "doublerait ses notifications : videz d'abord `GITHUB_WEBHOOK_SECRET` sur le serveur qui héberge Samae "
                "(voir `docs/github-app.md`). Rien n'a été relié."
            )
        return ""

    async def _preview_connection(self, interaction: discord.Interaction, repository: str) -> None:
        guild = await self._admin_still(interaction)
        if guild is None:
            return
        connected = self.bot.projects_repo.find(guild.id, "github", repository.casefold())
        if connected is not None and connected.id in self.bot.projects_repo.linked_ids(guild.id):
            # Déjà connecté : on le dit, et on propose le message de test si ses salons sont choisis.
            await self._show_connected(interaction, connected, interaction.response.send_message, ephemeral=True)
            return
        problem = self._connection_problem(guild.id, repository)
        if problem:
            await send_error(interaction, problem, title="Connexion impossible", tone="warning")
            return
        installation = self.bot.github_setup_repo.installation_for(guild.id, repository)
        # Orthographe donnée par GitHub (la saisie peut différer par les majuscules).
        repository = next(n for n in installation.repositories if n.casefold() == repository.casefold())
        existing = self.bot.projects_repo.find(guild.id, "github", repository.casefold())
        name = existing.name if existing else normalize_name(repository.split("/")[-1])
        author = interaction.user

        async def confirm(confirmation: discord.Interaction) -> None:
            # Revérifié : droits, installation et doublons ont pu changer pendant la confirmation.
            if not self._member_is_admin(confirmation.user):
                await confirmation.edit_original_response(content="Réservé aux administrateurs de ce serveur.", embeds=[], view=None)
                return
            again = self._connection_problem(guild.id, repository)
            if again:
                await confirmation.edit_original_response(content=again, embeds=[], view=None)
                return
            current = self.bot.github_setup_repo.installation_for(guild.id, repository)
            record = self.bot.projects_repo.find(guild.id, "github", repository.casefold())
            if record is None:
                record = self.bot.projects_repo.create(guild.id, "github", repository, name, created_by=author.id)
            self.bot.projects_repo.link_github(guild.id, record.id, current.installation_id)
            self.bot.record_admin_action(confirmation, "projet configurer", str(record.id), f"github {record.external_id}")
            await self._show_connected(confirmation, record, confirmation.edit_original_response, content=None)

        view = ConfirmView(author.id, confirm, command="/projet configurer")
        view.original = interaction
        embed = github_connect_preview_embed(
            repository, installation.account, guild.name, existing_ref=str(existing.id) if existing else None, name=name
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _show_connected(self, interaction: discord.Interaction, record, show, **extra) -> None:
        """État « connecté », avec le bouton du message de test si des salons sont choisis."""
        view = registered_view(record, self.bot.projects_repo.routes(record.guild_id, record.id), connected=True)
        test_view = TestMessageView(interaction.user.id, lambda inter: self._send_test(inter, record.id)) if view.routes else None
        if test_view is not None:
            test_view.original = interaction
            extra["view"] = test_view
        elif "content" in extra:  # modification d'un message : retirer les anciens boutons
            extra["view"] = None
        await show(embed=github_connected_embed(view), **extra)

    async def _send_test(self, interaction: discord.Interaction, project_id: int) -> None:
        """Message de test dans chaque salon du projet, après vérification du salon dans CE serveur."""
        guild = await self._admin_still(interaction)
        if guild is None:
            return
        record = self.bot.projects_repo.get(guild.id, project_id)
        routes = self.bot.projects_repo.routes(guild.id, project_id) if record else ()
        if not routes:
            await send_error(interaction, "Aucun salon n'est choisi pour ce projet : utilisez d'abord `/projet modifier`.", tone="warning")
            return
        by_channel: dict[int, list[str]] = {}
        for route in routes:
            if route.channel_id is not None:
                by_channel.setdefault(route.channel_id, []).append(route.category)
        # Plusieurs envois peuvent dépasser les 3 secondes accordées par Discord pour répondre.
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent, refused = [], []
        for channel_id, categories in by_channel.items():
            if self._channel_problem(guild, channel_id):  # salon d'un autre serveur, supprimé ou sans permission
                refused.append(f"<#{channel_id}>")
                continue
            channel = guild.get_channel(channel_id)
            try:
                message = await channel.send(embed=project_test_embed(record.name, categories))
            except discord.HTTPException:
                refused.append(f"<#{channel_id}>")
                continue
            # Envoi confirmé par Discord : le message renvoyé est bien dans CE salon de CE serveur.
            posted = getattr(message, "channel", None)
            if getattr(posted, "id", None) == channel_id and getattr(getattr(posted, "guild", None), "id", None) == guild.id:
                sent.append(f"<#{channel_id}>")
            else:
                refused.append(f"<#{channel_id}>")
        self.bot.record_admin_action(interaction, "projet tester", str(project_id), f"{len(sent)}/{len(by_channel)} salon(s)")
        lines = [f"Message de test envoyé et confirmé dans : {', '.join(sent)}." if sent else "Aucun message de test n'a pu être envoyé."]
        if refused:
            lines.append(f"Impossible de publier dans : {', '.join(refused)} (vérifiez le salon et les permissions de Samae).")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @projet.command(
        name="diagnostic",
        description="Admins : pourquoi un projet ne reçoit pas ses notifications (aucune modification)",
        extras={"admin": True},
    )
    @app_commands.describe(projet="Projet enregistré (référence affichée par /projet liste)")
    @admin_only()
    async def diagnostic(self, interaction: discord.Interaction, projet: app_commands.Range[str, 1, REF_MAX]) -> None:
        guild = await self._guild_or_error(interaction)
        if guild is None:
            return
        ref = projet.strip()
        record = self.bot.projects_repo.get(guild.id, int(ref)) if ref.isdigit() else None
        if record is None:
            legacy = " Les projets historiques du `.env` se vérifient avec `/admin diagnostic` et `/etat`." if not ref.isdigit() else ""
            await send_error(interaction, f"Projet enregistré introuvable sur ce serveur.{legacy}", title="Projet introuvable")
            return
        diagnosis = diagnose(self._project_facts(guild, record), self._reception_facts(), self._delivery_facts(record.id))
        view = registered_view(record, self.bot.projects_repo.routes(guild.id, record.id), connected=record.id in self.bot.projects_repo.linked_ids(guild.id))
        await interaction.response.send_message(embed=project_diagnosis_embed(view, diagnosis), ephemeral=True)

    def _reception_facts(self) -> ReceptionFacts:
        settings, server = self.bot.settings, self.bot.projects_server
        return ReceptionFacts(
            enabled=settings.projects_http_enabled,
            error=self.bot.projects_error,
            listening=server is not None and server.listening,
            github_open=bool(settings.github_app_webhook_secret),
        )

    def _project_facts(self, guild: discord.Guild, record) -> ProjectFacts:
        repo, settings = self.bot.projects_repo, self.bot.settings
        link = repo.link(guild.id, record.id)
        installation_known = link is not None and any(
            i.installation_id == link.installation_id for i in self.bot.github_setup_repo.installations(guild.id)
        )
        legacy_duplicate = guild.id == settings.guild_id and (
            (record.provider == "github" and settings.github_requested and settings.github_repository.casefold() == record.external_id)
            or (record.provider == "gitlab" and settings.gitlab_requested and str(settings.gitlab_project_id) == record.external_id)
        )
        return ProjectFacts(
            provider=record.provider,
            status=record.status,
            linked=link is not None,
            installation_known=installation_known,
            legacy_duplicate=legacy_duplicate,
            routes=[(route.category, self._channel_problem(guild, route.channel_id)) for route in repo.routes(guild.id, record.id)],
        )

    def _delivery_facts(self, project_id: int) -> DeliveryFacts:
        counts, last = self.bot.project_deliveries_repo.project_summary(project_id, DIAGNOSIS_DAYS)
        return DeliveryFacts(counts, last)

    @configure.autocomplete("depot")
    async def _repository_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Autocomplétion appelée sans les vérifications de la commande : administrateurs seulement.
        guild = interaction.guild
        if guild is None or guild.id not in self.project_guilds() or not self._is_admin(interaction):
            return []
        needle = current.casefold()
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in self._github_candidates(guild.id) if needle in n.casefold()][:25]

    @diagnostic.autocomplete("projet")
    @modify.autocomplete("projet")
    async def _registered_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Discord appelle l'autocomplétion sans passer par les vérifications de la commande : on refuse ici aussi.
        if not self._is_admin(interaction):
            return []
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
            for p in self.visible_projects(guild, interaction.user)
            if needle in p.name.casefold() or needle in p.ref
        ]
        return choices[:25]


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(ProjectsCog(bot))
