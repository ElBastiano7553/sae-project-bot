"""Client Discord principal : connexion, synchronisation des commandes, événements."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.config import ConfigError, Settings, load_links, load_planning, load_server_layout
from src.database import (
    ChannelBindingRepository,
    Database,
    GitLabEventRepository,
    KeyValueRepository,
    ManagedMessageRepository,
)
from src.discord_bot.gateway import DiscordMessageGateway
from src.discord_bot.gitlab_sender import DiscordNotificationSender
from src.discord_bot.snapshot import snapshot_guild
from src.gitlab.dispatcher import RETENTION_DAYS, NotificationDispatcher
from src.gitlab.rules import load_gitlab_rules
from src.gitlab.webhook import GitLabWebhookServer, create_webhook_app
from src.services.content import load_message_templates, render_message
from src.services.permanent_messages import PermanentMessageService, SyncAction
from src.services.server_structure import StructureReport, resolve_structure

log = logging.getLogger(__name__)

SETUP_CONFIRMED_KEY = "setup_confirmed_at"


class SaeBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        # Intents par défaut : aucun intent privilégié (ni contenu des messages, ni membres) n'est nécessaire.
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.settings = settings
        self.database = database
        self.messages_repo = ManagedMessageRepository(database)
        self.bindings_repo = ChannelBindingRepository(database)
        self.state_repo = KeyValueRepository(database)
        self.gateway = DiscordMessageGateway(self)
        self.gitlab_events_repo = GitLabEventRepository(database)
        self.gitlab_server: GitLabWebhookServer | None = None
        self.gitlab_error: str | None = None
        self._gitlab_task: asyncio.Task | None = None
        self.started_at = dt.datetime.now(dt.UTC)
        self._startup_done = False

    # --- Cycle de vie ------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.load_extension("src.discord_bot.cogs.admin")
        guild = discord.Object(id=self.settings.guild_id)
        # Synchronisation sur le serveur uniquement : les commandes apparaissent immédiatement.
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("%d commandes slash synchronisées sur le serveur %s", len(synced), self.settings.guild_id)
        await self.start_gitlab_webhooks()

    async def start_gitlab_webhooks(self) -> None:
        """Démarre la V2 si elle est configurée. Une erreur ici n'empêche jamais la V1 de fonctionner."""
        if self.settings.gitlab_config_error:
            self.gitlab_error = f"configuration invalide : {self.settings.gitlab_config_error}"
            log.error("Webhooks GitLab désactivés, %s", self.gitlab_error)
            return
        if not self.settings.gitlab_webhooks_enabled:
            log.info("Webhooks GitLab désactivés : GITLAB_WEBHOOK_SECRET n'est pas défini.")
            return
        try:
            await self._start_gitlab_webhooks()
        except ConfigError as exc:
            self.gitlab_error = str(exc)
            log.error("Webhooks GitLab désactivés : %s", exc)
        except Exception as exc:
            self.gitlab_error = f"erreur inattendue au démarrage ({type(exc).__name__}), voir logs/bot.log"
            log.exception("Webhooks GitLab désactivés : erreur inattendue au démarrage")
        if self.gitlab_error and self.gitlab_server is not None:
            await self.gitlab_server.stop()

    async def _start_gitlab_webhooks(self) -> None:
        rules = load_gitlab_rules(channel_keys=load_server_layout().channels.keys())
        purged = self.gitlab_events_repo.purge_older_than(RETENTION_DAYS)
        if purged:
            log.info("%d ancien(s) événement(s) GitLab oublié(s)", purged)
        dispatcher = NotificationDispatcher(
            self.gitlab_events_repo,
            rules,
            DiscordNotificationSender(self),
            self.bindings_repo.get_all,
            wait_until_ready=self.wait_until_ready,
        )
        app = create_webhook_app(
            secret=self.settings.gitlab_webhook_secret,
            project_id=self.settings.gitlab_project_id,
            dispatcher=dispatcher,
        )
        self.gitlab_server = GitLabWebhookServer(app, self.settings.webhook_host, self.settings.webhook_port)
        if not await self.gitlab_server.start():
            self.gitlab_error = self.gitlab_server.error
            return
        dispatcher.restore_pending()
        self._gitlab_task = asyncio.create_task(dispatcher.run(), name="gitlab-notifications")
        self._gitlab_task.add_done_callback(self._on_gitlab_task_done)

    def _on_gitlab_task_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.gitlab_error = "la tâche d'envoi des notifications s'est arrêtée, voir logs/bot.log"
            log.error("Tâche des notifications GitLab arrêtée", exc_info=task.exception())

    async def close(self) -> None:
        if self._gitlab_task is not None:
            self._gitlab_task.cancel()
            self._gitlab_task = None
        if self.gitlab_server is not None:
            await self.gitlab_server.stop()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (id %s)", self.user, self.user.id if self.user else "?")
        if self._startup_done:
            return  # on_ready peut être appelé plusieurs fois (reconnexions)
        self._startup_done = True
        guild = self.target_guild
        if guild is None:
            log.error(
                "Le bot n'est pas présent sur le serveur %s. Vérifiez DISCORD_GUILD_ID et invitez le bot.",
                self.settings.guild_id,
            )
            return
        try:
            report = self.resolve_structure(guild)
        except ConfigError as exc:
            log.error("%s", exc)
            return
        for problem in report.problems:
            log.warning("Salon « %s » : %s", problem.spec.name, problem.note or problem.status)
        for missing in report.missing_categories:
            log.warning("Catégorie « %s » : %s", missing.spec.name, missing.note)
        log.info("Structure : %d/%d salons utilisables", len(report.bindings), len(report.channels))
        if self.settings.auto_refresh_on_startup and self.setup_confirmed_at:
            await self._refresh_on_startup(report)

    async def _refresh_on_startup(self, report: StructureReport) -> None:
        try:
            results = await self.build_message_service().sync(report.bindings)
        except ConfigError as exc:
            log.error("Messages permanents non actualisés : %s", exc)
            return
        changed = [r for r in results if r.action is not SyncAction.UNCHANGED]
        log.info("Actualisation au démarrage : %d message(s) modifié(s) sur %d", len(changed), len(results))

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        managed = self.messages_repo.get_by_message_id(payload.message_id)
        if managed:
            log.warning(
                "Le message permanent « %s » a été supprimé. Utilisez /refresh pour le recréer.", managed.key
            )

    # --- Services ----------------------------------------------------------

    @property
    def target_guild(self) -> discord.Guild | None:
        return self.get_guild(self.settings.guild_id)

    @property
    def setup_confirmed_at(self) -> str | None:
        return self.state_repo.get(SETUP_CONFIRMED_KEY)

    def mark_setup_confirmed(self) -> None:
        self.state_repo.set(SETUP_CONFIRMED_KEY, dt.datetime.now().strftime("%d/%m/%Y %H:%M"))

    def resolve_structure(self, guild: discord.Guild) -> StructureReport:
        """Relit config/server.toml et compare avec le serveur (aucune modification)."""
        layout = load_server_layout()
        return resolve_structure(layout, snapshot_guild(guild), self.bindings_repo.get_all())

    def build_message_service(self) -> PermanentMessageService:
        """Relit les fichiers de contenu à chaque appel : /refresh prend en compte les modifications."""
        templates = load_message_templates()
        links = load_links()
        planning = load_planning()
        return PermanentMessageService(
            self.messages_repo,
            self.gateway,
            templates,
            lambda template: render_message(template, links, planning),
            pin_messages=self.settings.pin_permanent_messages,
        )


async def send_error(interaction: discord.Interaction, message: str) -> None:
    """Répond à l'utilisateur, que l'interaction ait déjà reçu une réponse ou non."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def install_error_handler(tree: app_commands.CommandTree) -> None:
    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CheckFailure):
            await send_error(interaction, "⛔ Cette commande est réservée aux administrateurs du bot.")
        elif isinstance(original, ConfigError):
            await send_error(interaction, f"⚠️ Problème de configuration :\n{original}")
        else:
            log.exception("Erreur pendant /%s", interaction.command.name if interaction.command else "?", exc_info=original)
            await send_error(interaction, "❌ Une erreur inattendue est survenue. Les détails sont dans `logs/bot.log`.")
