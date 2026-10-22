"""Client Discord principal : connexion, synchronisation des commandes, événements."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import signal
import threading
from dataclasses import asdict

import discord
from discord import app_commands
from discord.ext import commands

from src.ai import build_provider
from src.config import ConfigError, Settings, load_link_catalog, load_planning, load_server_layout, secret_values
from src.database import (
    AdminActionRepository,
    AIRequestRepository,
    BotErrorRepository,
    ChannelBindingRepository,
    Database,
    GitLabEventRepository,
    KeyValueRepository,
    ManagedMessageRepository,
)
from src.discord_bot.alerts import send_alert
from src.discord_bot.embeds import error_embed
from src.discord_bot.gateway import DiscordMessageGateway
from src.discord_bot.gitlab_sender import DiscordNotificationSender
from src.discord_bot.snapshot import snapshot_guild
from src.gitlab.api import GitLabAPI
from src.gitlab.dispatcher import DEFAULT_BRANCH_KEY, NotificationDispatcher
from src.gitlab.rules import load_gitlab_rules
from src.gitlab.webhook import GitLabWebhookServer, create_webhook_app
from src.services.ai_assistant import AIAssistant
from src.services.alerts import Alert, AlertManager
from src.services.backup import BackupError, BackupInfo, create_backup, list_backups
from src.services.content import Announcement, EmbedField, MessagePayload, load_message_templates, render_message
from src.services.dashboard import DASHBOARD_KEY, collect_dashboard, dashboard_fields
from src.services.monitoring import ErrorRecord, RecentErrors
from src.services.permanent_messages import PermanentMessageService, SyncAction
from src.services.server_structure import StructureReport, resolve_structure

log = logging.getLogger(__name__)

SETUP_CONFIRMED_KEY = "setup_confirmed_at"
ANNOUNCEMENT_KEY = "announcement"
EXTENSIONS = (
    "src.discord_bot.cogs.general",
    "src.discord_bot.cogs.activity",
    "src.discord_bot.cogs.ai",
    "src.discord_bot.cogs.admin",
)
# Délai avant de recréer un message permanent supprimé (regroupe les suppressions en rafale).
RECREATE_DELAY_SECONDS = 5.0
# Délai entre un événement GitLab et l'actualisation du tableau de bord (regroupe les rafales).
DASHBOARD_DEBOUNCE_SECONDS = 15.0
# Vérification périodique du tableau de bord (les statistiques « 7 jours » glissent avec le temps).
DASHBOARD_REFRESH_SECONDS = 30 * 60
# Nettoyage de l'historique (durée : HISTORY_RETENTION_DAYS).
PURGE_INTERVAL_SECONDS = 24 * 60 * 60
# Fichier de vie (contrôle de santé Docker) : réécrit à cet intervalle.
HEARTBEAT_SECONDS = 60


class SaeBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database, *, errors: RecentErrors | None = None) -> None:
        # Intents par défaut : aucun intent privilégié (ni contenu des messages, ni membres) n'est nécessaire.
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            # Protection globale : aucun message du bot ne peut notifier @everyone, un rôle ou un membre.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.database = database
        self.messages_repo = ManagedMessageRepository(database)
        self.bindings_repo = ChannelBindingRepository(database)
        self.state_repo = KeyValueRepository(database)
        self.admin_log = AdminActionRepository(database)
        self.gateway = DiscordMessageGateway(self)
        self.gitlab_events_repo = GitLabEventRepository(database)
        self.bot_errors_repo = BotErrorRepository(database)
        self.alerts = AlertManager(self.bot_errors_repo)
        self.ai_requests_repo = AIRequestRepository(database)
        # IA (lot 3) : None si désactivée. Aucun appel n'est fait au démarrage.
        self.ai_provider = build_provider(settings.ai_provider, settings.ai_api_key, settings.ai_model)
        self.assistant = (
            AIAssistant(
                self.ai_provider,
                self.ai_requests_repo,
                daily_limit=settings.ai_daily_limit,
                cooldown_seconds=settings.ai_cooldown_seconds,
                secrets=secret_values(),
            )
            if self.ai_provider is not None
            else None
        )
        # API GitLab en lecture seule (/review) : None sans GITLAB_ACCESS_TOKEN et GITLAB_PROJECT_ID.
        self.gitlab_api = (
            GitLabAPI(settings.gitlab_base_url, settings.gitlab_access_token, settings.gitlab_project_id)
            if settings.gitlab_api_enabled
            else None
        )
        self.gitlab_server: GitLabWebhookServer | None = None
        self.gitlab_error: str | None = None
        self.errors = errors or RecentErrors()
        self.errors.add_listener(self._on_error)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._dashboard_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._alert_tasks: set[asyncio.Task] = set()
        self._background: list[asyncio.Task] = []
        # Un seul verrou pour toutes les opérations sur les messages permanents (voir permanent_messages).
        self.messages_lock = asyncio.Lock()
        self._recreate_tasks: dict[str, asyncio.Task] = {}
        self._gitlab_task: asyncio.Task | None = None
        self.started_at = dt.datetime.now(dt.UTC)
        self._startup_done = False

    # --- Cycle de vie ------------------------------------------------------

    async def setup_hook(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._install_signal_handlers()
        self.purge_history()
        self._background.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        if self.settings.backup_interval_hours:
            self._background.append(asyncio.create_task(self._backup_loop(), name="backups"))
        for extension in EXTENSIONS:
            await self.load_extension(extension)
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
        dispatcher = NotificationDispatcher(
            self.gitlab_events_repo,
            rules,
            DiscordNotificationSender(self),
            self.bindings_repo.get_all,
            wait_until_ready=self.wait_until_ready,
            state=self.state_repo,
            on_event=self.schedule_dashboard_refresh,
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
        tasks = [*self._recreate_tasks.values(), *self._alert_tasks, *self._background, self._dashboard_task, self._maintenance_task]
        for task in tasks:
            if task is not None:
                task.cancel()
        self._recreate_tasks.clear()
        self._alert_tasks.clear()
        self._background.clear()
        self._dashboard_task = self._maintenance_task = None
        if self._gitlab_task is not None:
            self._gitlab_task.cancel()
            self._gitlab_task = None
        if self.gitlab_server is not None:
            await self.gitlab_server.stop()
        for client in (self.ai_provider, self.gitlab_api):
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    log.warning("Fermeture d'un client externe impossible", exc_info=True)
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
        if self.settings.alerts_channel and self.settings.alerts_channel not in report.bindings:
            log.warning(
                "Alertes désactivées : le salon ALERTS_CHANNEL=%s n'est pas associé (clé de config/server.toml, puis /setup).",
                self.settings.alerts_channel,
            )
        self._maintenance_task = asyncio.create_task(self._maintenance_loop(), name="maintenance")

    async def _refresh_on_startup(self, report: StructureReport) -> None:
        try:
            results = await self.build_message_service().sync(report.bindings)
        except ConfigError as exc:
            log.error("Messages permanents non actualisés : %s", exc)
            return
        changed = [r for r in results if r.action is not SyncAction.UNCHANGED]
        log.info("Actualisation au démarrage : %d message(s) modifié(s) sur %d", len(changed), len(results))

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        self.handle_deleted_messages([payload.message_id])

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        self.handle_deleted_messages(payload.message_ids)

    def handle_deleted_messages(self, message_ids) -> list[str]:
        """Recrée (après un court délai) les messages permanents supprimés à la main.

        Les suppressions faites par le bot lui-même (déplacement, reconstruction) ne déclenchent rien :
        la base désigne déjà le nouveau message au moment où l'ancien est supprimé.
        """
        keys = []
        for message_id in message_ids:
            managed = self.messages_repo.get_by_message_id(message_id)
            if managed is None:
                continue
            keys.append(managed.key)
            if not (self.settings.auto_recreate_deleted and self.setup_confirmed_at):
                log.warning("Le message permanent « %s » a été supprimé. Utilisez /refresh pour le recréer.", managed.key)
                continue
            if managed.key in self._recreate_tasks and not self._recreate_tasks[managed.key].done():
                continue
            log.warning("Le message permanent « %s » a été supprimé : il va être recréé.", managed.key)
            task = asyncio.create_task(self._recreate_later(managed.key), name=f"recreate-{managed.key}")
            self._recreate_tasks[managed.key] = task
        return keys

    async def _recreate_later(self, key: str) -> None:
        try:
            await asyncio.sleep(RECREATE_DELAY_SECONDS)
            guild = self.target_guild
            if guild is None:
                return
            report = self.resolve_structure(guild)
            results = await self.build_message_service().sync(report.bindings, keys=[key])
            for result in results:
                log.info("Message permanent %s après suppression : %s %s", key, result.action, result.detail)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Impossible de recréer le message permanent %s", key)
        finally:
            self._recreate_tasks.pop(key, None)

    # --- Tableau de bord ------------------------------------------------------

    def schedule_dashboard_refresh(self, delay: float = DASHBOARD_DEBOUNCE_SECONDS) -> bool:
        """Actualise le tableau de bord après ``delay`` secondes ; une rafale d'événements = une seule mise à jour."""
        if self._dashboard_task is not None and not self._dashboard_task.done():
            return False
        self._dashboard_task = asyncio.create_task(self._refresh_dashboard_later(delay), name="dashboard-refresh")
        return True

    async def _refresh_dashboard_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.refresh_dashboard()

    async def refresh_dashboard(self) -> str | None:
        """Met à jour le message du tableau de bord sur place. Retourne l'action effectuée, ou ``None``."""
        if not self.setup_confirmed_at or self.target_guild is None:
            return None
        try:
            report = self.resolve_structure(self.target_guild)
            results = await self.build_message_service().sync(report.bindings, keys=[DASHBOARD_KEY])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Impossible d'actualiser le tableau de bord")
            return None
        for result in results:
            if result.action is not SyncAction.UNCHANGED:
                log.info("Tableau de bord : %s %s", result.action, result.detail)
        return str(results[0].action) if results else None

    def dashboard_fields(self) -> list[EmbedField]:
        data = collect_dashboard(
            self.gitlab_events_repo,
            default_branch=self.state_repo.get(DEFAULT_BRANCH_KEY),
            gitlab_enabled=self.settings.gitlab_webhooks_enabled,
        )
        return dashboard_fields(data)

    # --- Maintenance -------------------------------------------------------------

    async def _maintenance_loop(self) -> None:
        elapsed = 0
        while True:
            await asyncio.sleep(DASHBOARD_REFRESH_SECONDS)
            elapsed += DASHBOARD_REFRESH_SECONDS
            try:
                if elapsed >= PURGE_INTERVAL_SECONDS:
                    elapsed = 0
                    self.purge_history()
                await self.refresh_dashboard()
            except asyncio.CancelledError:
                raise
            except Exception:  # la boucle de maintenance ne doit jamais s'arrêter
                log.exception("Erreur pendant la maintenance périodique")

    def purge_history(self) -> dict[str, int]:
        """Oublie l'historique plus ancien que HISTORY_RETENTION_DAYS (jamais moins de 30 jours)."""
        days = self.settings.history_retention_days
        purged: dict[str, int] = {}
        for label, repository in (
            ("événement(s) GitLab", self.gitlab_events_repo),
            ("action(s) d'administration", self.admin_log),
            ("erreur(s)", self.bot_errors_repo),
            ("demande(s) à l'IA", self.ai_requests_repo),
        ):
            try:
                purged[label] = repository.purge_older_than(days)
            except Exception:  # un nettoyage raté ne doit pas empêcher le bot de démarrer
                log.exception("Nettoyage de l'historique impossible (%s)", label)
                continue
            if purged[label]:
                log.info("Historique : %d %s de plus de %d jours oublié(s)", purged[label], label, days)
        return purged

    # --- Fiabilité : arrêt propre, fichier de vie, sauvegardes ------------------------

    def _install_signal_handlers(self) -> None:
        """SIGTERM (``docker stop``, systemd) : arrêt propre au lieu d'une interruption brutale."""
        try:
            self._loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except (NotImplementedError, RuntimeError, AttributeError):  # Windows : Ctrl+C suffit
            pass

    def _on_sigterm(self) -> None:
        log.info("Signal d'arrêt reçu : fermeture propre du bot")
        asyncio.get_running_loop().create_task(self.close(), name="shutdown")

    def write_heartbeat(self) -> None:
        """Écrit l'état courant dans le fichier de vie (lu par ``python -m src.healthcheck``)."""
        path = self.settings.heartbeat_path
        if path is None:
            return
        data = {"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "ready": self.is_ready(), "pid": os.getpid()}
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(data), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            log.warning("Fichier de vie %s impossible à écrire : %s", path, exc.strerror)

    async def _heartbeat_loop(self) -> None:
        while True:
            self.write_heartbeat()
            await asyncio.sleep(HEARTBEAT_SECONDS)

    def run_backup(self, reason: str = "") -> BackupInfo:
        """Sauvegarde vérifiée de la base, avec rotation (BACKUP_KEEP). Lève ``BackupError``."""
        if self.database.in_memory:
            raise BackupError("la base est en mémoire (tests) : rien à sauvegarder")
        info = create_backup(self.database.conn, self.settings.backup_dir, keep=self.settings.backup_keep, reason=reason)
        log.info("Sauvegarde de la base : %s (%d octets)", info.path.name, info.size)
        return info

    def last_backup(self) -> BackupInfo | None:
        backups = list_backups(self.settings.backup_dir) if self.settings.backup_dir else []
        return backups[0] if backups else None

    async def _backup_loop(self) -> None:
        interval = dt.timedelta(hours=self.settings.backup_interval_hours)
        while True:
            last = self.last_backup()
            due = last is None or dt.datetime.now(dt.UTC) - last.created_at >= interval
            if due:
                try:
                    self.run_backup()
                except BackupError as exc:
                    log.error("Sauvegarde automatique échouée : %s", exc)
                except Exception:
                    log.exception("Sauvegarde automatique échouée")
            await asyncio.sleep(min(interval.total_seconds(), 3600))

    # --- Erreurs et alertes --------------------------------------------------------

    def _on_error(self, record: ErrorRecord) -> None:
        """Appelé pour chaque erreur journalisée : historique en base et, si besoin, alerte."""
        if self._loop is None or self._loop.is_closed():
            return
        if threading.get_ident() == self._loop_thread:
            self.handle_error(record)
        else:  # erreur journalisée depuis un autre fil : SQLite n'est utilisé que depuis la boucle
            self._loop.call_soon_threadsafe(self.handle_error, record)

    def handle_error(self, record: ErrorRecord) -> Alert | None:
        alert = self.alerts.observe(record.source, record.message)
        if alert is None:
            return None
        channel_id = self.bindings_repo.get_all().get(self.settings.alerts_channel or "")
        if channel_id is None:
            return alert  # alertes désactivées : l'erreur reste visible avec /events type:erreurs
        task = asyncio.get_running_loop().create_task(send_alert(self, channel_id, alert), name="alert")
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)
        return alert

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
        catalog = load_link_catalog()
        planning = load_planning()
        announcement = self.announcement
        dashboard = self.dashboard_fields()
        return PermanentMessageService(
            self.messages_repo,
            self.gateway,
            templates,
            lambda template: render_message(template, catalog, planning, announcement=announcement, dashboard=dashboard),
            pin_messages=self.settings.pin_permanent_messages,
            lock=self.messages_lock,
        )

    def render_permanent(self, key: str, *, announcement: Announcement | None = None) -> MessagePayload:
        """Contenu d'un message permanent, pour une prévisualisation (rien n'est publié)."""
        template = next((t for t in load_message_templates() if t.key == key), None)
        if template is None:
            raise ConfigError(f"content/messages/{key}.toml est introuvable.")
        return render_message(
            template,
            load_link_catalog(),
            load_planning(),
            announcement=announcement or self.announcement,
            dashboard=self.dashboard_fields() if template.dynamic == "dashboard" else None,
        )

    # --- Annonce en cours (/announce) ------------------------------------------

    @property
    def announcement(self) -> Announcement | None:
        raw = self.state_repo.get(ANNOUNCEMENT_KEY)
        if not raw:
            return None
        try:
            return Announcement(**json.loads(raw))
        except (TypeError, ValueError):
            log.error("Annonce enregistrée illisible : elle est ignorée")
            return None

    def save_announcement(self, announcement: Announcement) -> None:
        self.state_repo.set(ANNOUNCEMENT_KEY, json.dumps(asdict(announcement), ensure_ascii=False))

    # --- Journal d'administration -------------------------------------------------

    def record_admin_action(self, interaction: discord.Interaction, action: str, target: str = "", outcome: str = "") -> None:
        try:
            self.admin_log.record(interaction.user.id, str(interaction.user), action, target, outcome)
        except Exception:  # le journal ne doit jamais empêcher l'action elle-même
            log.exception("Impossible d'enregistrer l'action d'administration %s", action)


async def send_error(interaction: discord.Interaction, message: str, *, title: str = "Action impossible", tone: str = "failure") -> None:
    """Répond à l'utilisateur, que l'interaction ait déjà reçu une réponse ou non."""
    embed = error_embed(message, title=title, tone=tone)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.warning("Impossible d'afficher le message d'erreur à l'utilisateur", exc_info=True)


def install_error_handler(tree: app_commands.CommandTree) -> None:
    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        name = interaction.command.qualified_name if interaction.command else "?"
        if isinstance(error, app_commands.NoPrivateMessage):
            await send_error(interaction, "Cette commande s'utilise sur le serveur de l'équipe, pas en message privé.")
        elif isinstance(error, app_commands.CheckFailure):
            await send_error(
                interaction,
                "Cette commande est réservée aux administrateurs du bot (permission « Gérer le serveur » "
                "ou rôle défini dans ADMIN_ROLE_NAME).",
                title="Accès refusé",
            )
        elif isinstance(original, ConfigError):
            await send_error(interaction, str(original), title="Problème de configuration", tone="warning")
        elif isinstance(original, discord.Forbidden):
            log.error("Permission Discord manquante pendant /%s : %s", name, original.text)
            await send_error(interaction, "Le bot n'a pas la permission nécessaire. Un administrateur peut lancer `/admin diagnostic`.")
        else:
            log.error("Erreur pendant /%s : %s", name, type(original).__name__, exc_info=original)
            await send_error(interaction, "Une erreur inattendue est survenue. Les administrateurs en trouveront le détail dans les journaux du bot.")
