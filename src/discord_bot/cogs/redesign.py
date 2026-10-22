"""Commande /refonte : réorganiser le serveur selon config/server.toml, après aperçu et confirmation.

Étapes : analyse (lecture seule) → aperçu paginé → confirmation explicite →
revérification (auteur, permissions, état du serveur) → sauvegarde de la base →
application → enregistrement des associations → rapport fidèle.

Sécurité : la commande est masquée par défaut (« Gérer le serveur »), revérifiée
côté bot (administrateur du bot), et l'application exige en plus que la personne
ait elle-même « Gérer les salons » (et « Gérer les rôles » si des rôles sont créés) :
Samae ne fait jamais pour quelqu'un ce qu'il ne pourrait pas faire lui-même.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from src.config import ConfigError, load_server_layout
from src.discord_bot.bot import SaeBot, send_error
from src.discord_bot.cogs.admin import admin_only
from src.discord_bot.embeds import diagnostic_embed, error_embed
from src.discord_bot.permissions import is_bot_admin
from src.discord_bot.redesign import RedesignExecutor
from src.discord_bot.redesign_ui import (
    ANALYSIS,
    CLOSED,
    CONFIRM,
    DONE,
    PREVIEW,
    RUNNING,
    RedesignSession,
    RedesignView,
    running_embed,
)
from src.discord_bot.snapshot import snapshot_guild
from src.services.backup import BackupError
from src.services.meetings import FINISHED, OPEN_STATUSES
from src.services.server_redesign import (
    OperationResult,
    OpKind,
    RedesignOptions,
    RedesignOutcome,
    ResultStatus,
    analyse,
    build_plan,
)

log = logging.getLogger(__name__)

JOURNAL_DIR = "refonte"


class RedesignCog(commands.Cog):
    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot

    # --- Commande -------------------------------------------------------------------------

    @app_commands.command(name="refonte", description="Analyser le serveur, prévisualiser puis appliquer la structure cible (aucune suppression)")
    @app_commands.describe(
        facultatifs="Créer aussi les salons facultatifs absents (non par défaut)",
        roles="Créer les rôles de projet absents, sans aucune permission (non par défaut)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @admin_only()
    async def refonte(self, interaction: discord.Interaction, facultatifs: bool = False, roles: bool = False) -> None:
        options = RedesignOptions(create_optional=facultatifs, create_roles=roles)
        problem = self.authorization_problem(interaction.user, options)
        if problem:
            await send_error(interaction, problem, title="Refonte impossible", tone="warning")
            return
        guild = self._guild(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        session = self.new_session(guild, interaction.user.id, options)
        view = RedesignView(self, session)
        view.original = interaction
        await interaction.followup.send(embed=view.current_embed(), view=view, ephemeral=True)

    # --- Analyse (aucun effet de bord) ---------------------------------------------------------

    def new_session(self, guild: discord.Guild, author_id: int, options: RedesignOptions) -> RedesignSession:
        plan, analysis, context = self.analyse(guild, options)
        return RedesignSession(author_id=author_id, options=options, plan=plan, analysis=analysis, context=context)

    def analyse(self, guild: discord.Guild, options: RedesignOptions):
        """Lit le serveur et la configuration, calcule le plan. Ne modifie rien, ni sur Discord ni en base."""
        snapshot = snapshot_guild(guild)
        plan = build_plan(
            load_server_layout(),
            snapshot,
            saved_bindings=self.bot.bindings_repo.get_all(),
            saved_categories=self.bot.category_bindings,
            options=options,
            alerts_channel=self.bot.settings.alerts_channel,
        )
        return plan, analyse(snapshot), self._samae_context()

    def _samae_context(self) -> list[str]:
        bindings = self.bot.bindings_repo.get_all()
        messages = self.bot.messages_repo.list_all()
        meetings = self.bot.meetings.repo
        open_count = len(meetings.list(OPEN_STATUSES, limit=1000))
        finished = len(meetings.list((FINISHED,), limit=1000))
        setup = self.bot.setup_confirmed_at
        return [
            f"{len(bindings)} salon(s) associé(s) à une fonction · {len(messages)} message(s) permanent(s) publiés"
            + (f" · /setup confirmé le {setup}" if setup else " · /setup jamais confirmé"),
            f"Réunions : {open_count} prévue(s) ou en cours, {finished} terminée(s). Panneaux et comptes rendus sont "
            "retrouvés par identifiant : un renommage ou un déplacement ne les casse pas.",
        ]

    # --- Contrôles d'accès ---------------------------------------------------------------------

    def authorization_problem(self, user: discord.abc.User, options: RedesignOptions) -> str | None:
        """Vérifié à l'ouverture ET au moment d'appliquer (les droits ont pu changer entre-temps)."""
        if not isinstance(user, discord.Member) or not is_bot_admin(user, self.bot.settings.admin_role_name):
            return "La refonte est réservée aux administrateurs du bot (« Gérer le serveur » ou rôle ADMIN_ROLE_NAME)."
        perms = user.guild_permissions
        if not (perms.administrator or perms.manage_channels):
            return "Vous devez avoir vous-même la permission Discord « Gérer les salons » pour réorganiser le serveur."
        if options.create_roles and not (perms.administrator or perms.manage_roles):
            return "L'option roles:True demande que vous ayez vous-même la permission Discord « Gérer les rôles »."
        return None

    # --- Boutons ---------------------------------------------------------------------------------

    async def handle_redesign_button(self, interaction: discord.Interaction, view: RedesignView, action: str) -> None:
        session = view.session
        if action in ("prev", "next"):
            session.page += -1 if action == "prev" else 1
            await view.show(interaction)
        elif action == "analyse":
            plan, analysis, context = self.analyse(self._guild(interaction), session.options)
            changed = plan.fingerprint != session.plan.fingerprint
            session.plan, session.analysis, session.context = plan, analysis, context
            session.mode, session.page = ANALYSIS, 0
            session.notice = "Nouvelle analyse : le plan a changé." if changed else "Nouvelle analyse : plan inchangé."
            await view.show(interaction)
        elif action == "preview":
            session.mode, session.page, session.notice = PREVIEW, 0, ""
            await view.show(interaction)
        elif action == "apply":
            if not session.plan.can_apply:
                await send_error(interaction, "Ce plan ne peut pas être appliqué : voir « Bloquant » dans l'aperçu.", tone="warning")
                return
            session.mode, session.page = CONFIRM, 0
            await view.show(interaction)
        elif action == "confirm":
            await self.confirm(interaction, view)
        elif action == "cancel":
            session.mode = CLOSED
            view.stop()
            await interaction.response.edit_message(content="Refonte annulée : rien n'a été modifié.", embed=None, view=None)

    # --- Application -----------------------------------------------------------------------------

    async def confirm(self, interaction: discord.Interaction, view: RedesignView) -> None:
        session = view.session
        problem = self.authorization_problem(interaction.user, session.options)
        if problem:
            await send_error(interaction, problem, title="Refonte refusée", tone="warning")
            return
        if self.bot.redesign_lock.locked():
            await send_error(interaction, "Une refonte est déjà en cours d'application : attendez son rapport.", tone="warning")
            return
        async with self.bot.redesign_lock:
            session.mode = RUNNING
            await interaction.response.edit_message(content=None, embed=running_embed(), view=None)
            guild = self._guild(interaction)
            try:
                plan, analysis, context = self.analyse(guild, session.options)
            except ConfigError as exc:
                session.mode = ANALYSIS
                await interaction.edit_original_response(embed=error_embed(str(exc), title="Problème de configuration", tone="warning"), view=None)
                return
            if plan.fingerprint != session.plan.fingerprint:
                # Le serveur ou la configuration ont changé depuis l'aperçu : on n'applique rien.
                session.plan, session.analysis, session.context = plan, analysis, context
                session.mode, session.page = PREVIEW, 0
                session.notice = (
                    "**Le serveur a changé depuis l'aperçu** : rien n'a été appliqué. Voici le plan recalculé ; "
                    "vérifiez-le puis confirmez à nouveau."
                )
                await view.show(interaction)
                return
            if not plan.can_apply:
                session.mode = PREVIEW
                session.notice = "Ce plan ne peut pas être appliqué (voir « Bloquant »)."
                await view.show(interaction)
                return
            outcome = await self.apply(guild, plan, interaction)
            session.outcome = outcome
            session.outcome_extra = [self._destinations_embed(guild)]
            session.mode, session.page, session.notice = DONE, 0, ""
            await view.show(interaction)

    async def apply(self, guild: discord.Guild, plan, interaction: discord.Interaction) -> RedesignOutcome:
        backup = None
        if not self.bot.database.in_memory:
            try:
                backup = self.bot.run_backup(reason="avant-refonte").path.name
            except BackupError as exc:
                # Pas de filet de sécurité : on n'applique rien.
                self.bot.record_admin_action(interaction, "refonte", plan.fingerprint, f"annulée : sauvegarde impossible ({exc})")
                results = tuple(OperationResult(op, ResultStatus.SKIPPED, "non appliquée : sauvegarde impossible") for op in plan.actions)
                return RedesignOutcome(results, tuple(plan.of(OpKind.MANUAL)))
        log.info("/refonte confirmée par %s : plan %s, %d opération(s)", interaction.user, plan.fingerprint, len(plan.actions))
        executor = RedesignExecutor(guild, actor=str(interaction.user), admin_role_name=self.bot.settings.admin_role_name)
        results = await executor.apply(plan)
        results += self._save_configuration(plan, results, executor.categories)
        outcome = RedesignOutcome(tuple(results), tuple(plan.of(OpKind.MANUAL)), backup=backup)
        journal = self._write_journal(plan, outcome, interaction)
        if journal:
            outcome = RedesignOutcome(outcome.results, outcome.manual, backup=backup, journal=journal)
        done = len(outcome.with_status(ResultStatus.DONE))
        failed = len(outcome.with_status(ResultStatus.FAILED))
        self.bot.record_admin_action(interaction, "refonte", plan.fingerprint, f"{done} réussie(s), {failed} échouée(s)")
        return outcome

    def _save_configuration(self, plan, results: list[OperationResult], categories: dict[str, int]) -> list[OperationResult]:
        """Enregistre les associations « fonction → salon » d'après les identifiants (jamais d'après les noms)."""
        created = {r.operation.key: r.new_id for r in results if r.operation.kind is OpKind.CREATE_CHANNEL and r.new_id}
        bindings = dict(plan.report.bindings) if plan.report else {}
        bindings.update(created)
        self.bot.bindings_repo.replace_all(bindings)
        self.bot.save_category_bindings(categories)
        saved = []
        for op in plan.of(OpKind.BIND):
            if op.key in bindings:
                saved.append(OperationResult(op, ResultStatus.DONE, "enregistrée"))
            elif op.object_id is None and op.current:
                saved.append(OperationResult(op, ResultStatus.DONE, "ancienne association retirée"))
            else:
                saved.append(OperationResult(op, ResultStatus.SKIPPED, "salon non créé : association non enregistrée"))
        return saved

    def _write_journal(self, plan, outcome: RedesignOutcome, interaction: discord.Interaction) -> str | None:
        """Journal JSON de la refonte (état avant, opérations, résultats) pour diagnostiquer et revenir en arrière."""
        if self.bot.database.in_memory:
            return None
        directory = Path(self.bot.settings.database_path).parent / JOURNAL_DIR
        now = dt.datetime.now(dt.UTC)
        path = directory / f"refonte-{now:%Y%m%d-%H%M%S}.json"
        data = {
            "at": now.isoformat(timespec="seconds"),
            "user": {"id": interaction.user.id, "name": str(interaction.user)},
            "plan": plan.fingerprint,
            "options": asdict(plan.options),
            "backup": outcome.backup,
            "before": {
                "categories": [asdict(c) for c in plan.report.categories] if plan.report else [],
                "channels": [asdict(c) for c in plan.report.channels] if plan.report else [],
            },
            "operations": [asdict(op) for op in plan.operations],
            "results": [
                {"operation": asdict(r.operation), "status": r.status, "detail": r.detail, "new_id": r.new_id} for r in outcome.results
            ],
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            log.error("Journal de refonte impossible à écrire dans %s : %s", directory, exc.strerror)
            return None
        return path.name

    def _destinations_embed(self, guild: discord.Guild) -> discord.Embed:
        """Vérifie, après coup, que chaque salon utilisé par Samae est associé et accessible."""
        admin = self.bot.get_cog("AdminCog")
        bindings = self.bot.bindings_repo.get_all()
        embed = diagnostic_embed(admin._diagnose_channels(guild, bindings) if admin else [])
        embed.title = "Vérification des destinations"
        lines = []
        for key, label in (("comptes_rendus", "Comptes rendus de réunion"), ("logs", "Alertes (si ALERTS_CHANNEL=logs)")):
            lines.append(f"{label} : " + (f"<#{bindings[key]}>" if key in bindings else "salon non associé"))
        embed.add_field(name="Autres destinations", value="\n".join(lines), inline=False)
        return embed

    def _guild(self, interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None or interaction.guild.id != self.bot.settings.guild_id:
            raise ConfigError("Cette commande doit être utilisée sur le serveur configuré dans DISCORD_GUILD_ID.")
        return interaction.guild


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(RedesignCog(bot))
