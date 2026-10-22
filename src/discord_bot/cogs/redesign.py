"""Commandes /refonte : réorganiser le serveur selon config/server.toml, et revenir en arrière.

* ``/refonte lancer`` : analyse (lecture seule) → aperçu paginé → confirmation →
  revérification (auteur, permissions, état du serveur) → sauvegarde de la base
  et relevé du serveur → application → enregistrement des associations →
  vérification → rapport. Les suppressions, seulement avec ``suppressions:True``,
  se font ensuite à part : examen, puis confirmation distincte.
* ``/refonte retour`` : rétablit ce qu'une refonte a changé, d'après son journal.
* ``/refonte journal`` : les dernières refontes et retours arrière.

Sécurité : les commandes sont masquées par défaut (« Gérer le serveur »),
revérifiées côté bot (administrateur du bot), et exigent en plus que la personne
ait elle-même « Gérer les salons ». Les opérations sur les rôles et les
permissions demandent aussi qu'elle ait « Gérer les rôles » : sinon elles sont
listées comme impossibles, jamais faites à sa place.
"""

from __future__ import annotations

import asyncio
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
    DELETE_CONFIRM,
    DELETIONS,
    DONE,
    PREVIEW,
    REFONTE,
    RETOUR,
    RUNNING,
    RedesignSession,
    RedesignView,
    running_embed,
)
from src.discord_bot.snapshot import permission_names, snapshot_guild
from src.services.backup import BackupError
from src.services.meetings import FINISHED, OPEN_STATUSES
from src.services.redesign_rollback import build_rollback_plan
from src.services.server_redesign import (
    Actor,
    OperationResult,
    OpKind,
    RedesignOptions,
    RedesignOutcome,
    RedesignPlan,
    ResultStatus,
    analyse,
    build_plan,
    describe,
)

log = logging.getLogger(__name__)

JOURNAL_DIR = "refonte"
# Délai laissé à Discord pour diffuser les changements avant la vérification finale.
SETTLE_DELAY = 2.0
JOURNAL_LIST = 10


class RedesignJournal:
    """Journal JSON d'une refonte : réécrit après chaque opération, il survit à une interruption."""

    def __init__(self, path: Path | None, data: dict) -> None:
        self.path = path
        self.data = data

    def add(self, result: OperationResult) -> None:
        self.data.setdefault("results", []).append(_serialize_result(result))
        self.save()

    def finish(self, status: str, **extra) -> None:
        self.data["status"] = status
        self.data.update(extra)
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            log.error("Journal de refonte impossible à écrire dans %s : %s", self.path.parent, exc.strerror)
            self.path = None

    @property
    def name(self) -> str | None:
        return self.path.name if self.path else None


def _serialize_result(result: OperationResult) -> dict:
    return {
        "operation": asdict(result.operation),
        "status": str(result.status),
        "detail": result.detail,
        "new_id": result.new_id,
        "undo": result.undo,
    }


class RedesignCog(commands.Cog):
    refonte = app_commands.Group(
        name="refonte",
        description="Réorganiser le serveur selon la structure cible, ou revenir en arrière",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SaeBot) -> None:
        self.bot = bot
        self.settle_delay = SETTLE_DELAY
        database_path = Path(bot.settings.database_path)
        self.journal_dir: Path | None = None if bot.database.in_memory else database_path.parent / JOURNAL_DIR
        self.last_journal: dict | None = None  # dernier journal écrit (utile quand la base est en mémoire)

    # --- Commandes -------------------------------------------------------------------------

    @refonte.command(name="lancer", description="Analyser le serveur, prévisualiser puis appliquer la structure cible")
    @app_commands.describe(
        profil="Profil du serveur (par défaut : celui de config/server.toml)",
        style="Style d'affichage des noms : emojis, séparateurs (par défaut : celui du profil)",
        facultatifs="Créer aussi les salons et rôles facultatifs absents (non par défaut)",
        roles="Créer, compléter et ordonner les rôles (oui par défaut)",
        permissions="Corriger les permissions des catégories et salons (oui par défaut)",
        suppressions="Proposer les suppressions justifiées, sur confirmation séparée (non par défaut)",
    )
    @admin_only()
    async def lancer(
        self,
        interaction: discord.Interaction,
        profil: str | None = None,
        style: str | None = None,
        facultatifs: bool = False,
        roles: bool = True,
        permissions: bool = True,
        suppressions: bool = False,
    ) -> None:
        options = RedesignOptions(
            profile=profil or "", style=style or "", create_optional=facultatifs, manage_roles=roles,
            manage_permissions=permissions, deletions=suppressions,
        )
        problem = self.authorization_problem(interaction.user)
        if problem:
            await send_error(interaction, problem, title="Refonte impossible", tone="warning")
            return
        guild = self._guild(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            session = self.new_session(guild, interaction.user, options)
        except (ConfigError, ValueError) as exc:
            await interaction.followup.send(embed=error_embed(str(exc), title="Problème de configuration", tone="warning"), ephemeral=True)
            return
        view = RedesignView(self, session)
        view.original = interaction
        await interaction.followup.send(embed=view.current_embed(), view=view, ephemeral=True)

    @lancer.autocomplete("profil")
    async def _profile_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return self._choices("profile", current)

    @lancer.autocomplete("style")
    async def _style_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return self._choices("style", current)

    def _choices(self, name: str, current: str) -> list[app_commands.Choice[str]]:
        try:
            pairs = self.choices()[name]
        except ConfigError:
            return []
        wanted = current.casefold()
        return [app_commands.Choice(name=label[:100], value=value) for value, label in pairs if wanted in label.casefold() or wanted in value][:25]

    @refonte.command(name="retour", description="Rétablir ce qu'une refonte a changé, d'après son journal")
    @app_commands.describe(journal="Journal de la refonte à annuler (par défaut : le plus récent)")
    @admin_only()
    async def retour(self, interaction: discord.Interaction, journal: str | None = None) -> None:
        problem = self.authorization_problem(interaction.user)
        if problem:
            await send_error(interaction, problem, title="Retour arrière impossible", tone="warning")
            return
        data = self.load_journal(journal)
        if data is None:
            await send_error(interaction, "Aucun journal de refonte trouvé" + (f" sous le nom « {journal} »." if journal else "."),
                             title="Retour arrière impossible", tone="warning")
            return
        guild = self._guild(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = build_rollback_plan(data, snapshot_guild(guild))
        session = RedesignSession(author_id=interaction.user.id, options=plan.options, plan=plan, analysis=None,
                                  context=self._journal_context(data), kind=RETOUR, journal=data)
        view = RedesignView(self, session)
        view.original = interaction
        await interaction.followup.send(embed=view.current_embed(), view=view, ephemeral=True)

    @retour.autocomplete("journal")
    async def _journal_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=name, value=name) for name in self.journal_names() if current in name][:25]

    @refonte.command(name="journal", description="Dernières refontes, suppressions et retours arrière")
    @admin_only()
    async def journal(self, interaction: discord.Interaction) -> None:
        lines = []
        for name in self.journal_names()[:JOURNAL_LIST]:
            data = self.load_journal(name) or {}
            results = data.get("results", [])
            done = sum(1 for r in results if r.get("status") == "done")
            failed = sum(1 for r in results if r.get("status") == "failed")
            lines.append(f"`{name}` · {data.get('status', '?')} · {done} réussie(s), {failed} échouée(s) · "
                         f"{data.get('user', {}).get('name', '?')}")
        text = "\n".join(lines) if lines else "Aucune refonte enregistrée."
        embed = error_embed(text, title="Journal des refontes", tone="info")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Analyse (aucun effet de bord) ---------------------------------------------------------

    def choices(self) -> dict[str, list[tuple[str, str]]]:
        layout = load_server_layout()
        return {
            "profile": [(p.key, p.label) for p in layout.profiles.values()],
            "style": [(s.key, s.label) for s in layout.styles.values()],
        }

    def new_session(self, guild: discord.Guild, user: discord.abc.User, options: RedesignOptions) -> RedesignSession:
        layout = load_server_layout()
        options = RedesignOptions(**(asdict(options) | {
            "profile": options.profile or layout.policy.profile,
        }))
        plan, analysis, context = self.analyse(guild, options, user)
        if not options.style and plan.target is not None:
            options = RedesignOptions(**(asdict(options) | {"style": plan.target.style.key}))
            plan, analysis, context = self.analyse(guild, options, user)
        return RedesignSession(author_id=user.id, options=options, plan=plan, analysis=analysis, context=context, choices=self.choices())

    def analyse(self, guild: discord.Guild, options: RedesignOptions, user: discord.abc.User | None = None):
        """Lit le serveur et la configuration, calcule le plan. Ne modifie rien, ni sur Discord ni en base."""
        snapshot = snapshot_guild(guild)
        plan = build_plan(
            load_server_layout(),
            snapshot,
            saved_bindings=self.bot.bindings_repo.get_all(),
            saved_categories=self.bot.category_bindings,
            saved_roles=self.bot.role_bindings,
            options=options,
            alerts_channel=self.bot.settings.alerts_channel,
            admin_role_name=self.bot.settings.admin_role_name,
            actor=_actor(guild, user),
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
            f"{len(bindings)} salon(s) et {len(self.bot.role_bindings)} rôle(s) associés · {len(messages)} message(s) permanent(s)"
            + (f" · /setup confirmé le {setup}" if setup else " · /setup jamais confirmé"),
            f"Réunions : {open_count} prévue(s) ou en cours, {finished} terminée(s). Panneaux et comptes rendus sont "
            "retrouvés par identifiant : un renommage ou un déplacement ne les casse pas.",
        ]

    # --- Contrôles d'accès ---------------------------------------------------------------------

    def authorization_problem(self, user: discord.abc.User) -> str | None:
        """Vérifié à l'ouverture ET au moment d'appliquer (les droits ont pu changer entre-temps)."""
        if not isinstance(user, discord.Member) or not is_bot_admin(user, self.bot.settings.admin_role_name):
            return "La refonte est réservée aux administrateurs du bot (« Gérer le serveur » ou rôle ADMIN_ROLE_NAME)."
        perms = user.guild_permissions
        if not (perms.administrator or perms.manage_channels):
            return "Vous devez avoir vous-même la permission Discord « Gérer les salons » pour réorganiser le serveur."
        return None

    # --- Boutons et menus -------------------------------------------------------------------------

    async def handle_redesign_choice(self, interaction: discord.Interaction, view: RedesignView, name: str, value: str) -> None:
        session = view.session
        changes = {"profile": value, "style": ""} if name == "profile" else {"style": value}
        options = RedesignOptions(**(asdict(session.options) | changes))
        guild = self._guild(interaction)
        try:
            plan, analysis, context = self.analyse(guild, options, interaction.user)
            if name == "profile" and plan.target is not None:
                options = RedesignOptions(**(asdict(options) | {"style": plan.target.style.key}))
                plan, analysis, context = self.analyse(guild, options, interaction.user)
        except (ConfigError, ValueError) as exc:
            await send_error(interaction, str(exc), title="Problème de configuration", tone="warning")
            return
        session.options, session.plan, session.analysis, session.context = options, plan, analysis, context
        session.mode, session.page = ANALYSIS, 0
        session.notice = f"Plan recalculé pour le {'profil' if name == 'profile' else 'style'} choisi."
        await view.show(interaction)

    async def handle_redesign_button(self, interaction: discord.Interaction, view: RedesignView, action: str) -> None:
        session = view.session
        if action in ("prev", "next"):
            session.page += -1 if action == "prev" else 1
            await view.show(interaction)
        elif action == "analyse":
            plan, analysis, context = self._reanalyse(interaction, session)
            changed = plan.fingerprint != session.plan.fingerprint
            session.plan, session.context = plan, context
            session.analysis = analysis or session.analysis
            session.mode, session.page = ANALYSIS, 0
            session.notice = "Nouvelle analyse : le plan a changé." if changed else "Nouvelle analyse : plan inchangé."
            await view.show(interaction)
        elif action == "preview":
            session.mode, session.page, session.notice = PREVIEW, 0, ""
            await view.show(interaction)
        elif action == "apply":
            if not session.plan.can_apply or session.applied:
                await send_error(interaction, "Ce plan ne peut pas être appliqué : voir « Bloquant » dans l'aperçu.", tone="warning")
                return
            session.mode, session.page = CONFIRM, 0
            await view.show(interaction)
        elif action == "confirm":
            await self.confirm(interaction, view)
        elif action == "deletions":
            session.mode, session.page, session.notice = DELETIONS, 0, ""
            await view.show(interaction)
        elif action == "delete_review":
            session.mode, session.page = DELETE_CONFIRM, 0
            await view.show(interaction)
        elif action == "delete_confirm":
            await self.confirm_deletions(interaction, view)
        elif action in ("cancel", "close"):
            session.mode = CLOSED
            view.stop()
            text = "Fermé." if session.applied else "Annulé : rien n'a été modifié."
            await interaction.response.edit_message(content=text, embed=None, view=None)

    def _reanalyse(self, interaction: discord.Interaction, session: RedesignSession):
        guild = self._guild(interaction)
        if session.kind == RETOUR:
            return build_rollback_plan(session.journal or {}, snapshot_guild(guild)), None, session.context
        return self.analyse(guild, session.options, interaction.user)

    # --- Application -----------------------------------------------------------------------------

    async def _begin(self, interaction: discord.Interaction, session: RedesignSession) -> bool:
        problem = self.authorization_problem(interaction.user)
        if problem:
            await send_error(interaction, problem, title="Refusé", tone="warning")
            return False
        if self.bot.redesign_lock.locked():
            await send_error(interaction, "Une refonte est déjà en cours d'application : attendez son rapport.", tone="warning")
            return False
        return True

    async def confirm(self, interaction: discord.Interaction, view: RedesignView) -> None:
        session = view.session
        if session.applied or not await self._begin(interaction, session):
            return
        async with self.bot.redesign_lock:
            session.mode = RUNNING
            await interaction.response.edit_message(content=None, embed=running_embed(), view=None)
            guild = self._guild(interaction)
            try:
                plan, analysis, context = self._reanalyse(interaction, session)
            except (ConfigError, ValueError) as exc:
                session.mode = ANALYSIS
                await interaction.edit_original_response(embed=error_embed(str(exc), title="Problème de configuration", tone="warning"), view=None)
                return
            if plan.fingerprint != session.plan.fingerprint:
                # Le serveur ou la configuration ont changé depuis l'aperçu : rien n'est appliqué.
                differences = _differences(session.plan, plan)
                session.plan, session.context = plan, context
                session.analysis = analysis or session.analysis
                session.mode, session.page = PREVIEW, 0
                session.notice = ("**Le serveur a changé depuis l'aperçu** : rien n'a été appliqué. "
                                  + ("Différences : " + " ; ".join(differences) + ". " if differences else "")
                                  + "Vérifiez le plan recalculé puis confirmez à nouveau.")
                await view.show(interaction)
                return
            if not plan.can_apply:
                session.mode = PREVIEW
                session.notice = "Ce plan ne peut pas être appliqué (voir « Bloquant »)."
                await view.show(interaction)
                return
            session.applied = True
            outcome = await self.apply(guild, plan, interaction, session)
            session.outcome = outcome
            if session.kind == REFONTE:
                session.outcome_extra = [self._destinations_embed(guild)]
                # Les suppressions éventuelles sont recalculées sur le serveur réorganisé.
                session.plan, _, _ = self.analyse(guild, session.options, interaction.user)
            session.mode, session.page, session.notice = DONE, 0, ""
            await view.show(interaction)

    async def apply(self, guild: discord.Guild, plan: RedesignPlan, interaction: discord.Interaction,
                    session: RedesignSession | None = None, *, deletions: bool = False) -> RedesignOutcome:
        kind = session.kind if session else REFONTE
        label = "suppressions" if deletions else kind
        operations = list(plan.deletions) if deletions else plan.actions
        title = {"suppressions": "Suppressions", RETOUR: "Retour arrière", REFONTE: "Refonte"}[label]
        action = {"suppressions": "refonte suppressions", RETOUR: "refonte retour", REFONTE: "refonte"}[label]
        backup = None
        if not self.bot.database.in_memory:
            try:
                backup = self.bot.run_backup(reason=f"avant-{label}").path.name
            except BackupError as exc:
                # Pas de filet de sécurité : rien n'est appliqué.
                self.bot.record_admin_action(interaction, action, plan.fingerprint, f"annulée : sauvegarde impossible ({exc})")
                results = tuple(OperationResult(op, ResultStatus.SKIPPED, "non appliquée : sauvegarde impossible") for op in operations)
                return RedesignOutcome(results, tuple(plan.of(OpKind.MANUAL)), title=title)
        journal = self._start_journal(label, guild, plan, interaction, backup, session)
        log.info("/refonte (%s) confirmée par %s : plan %s, %d opération(s)", label, interaction.user, plan.fingerprint, len(operations))

        async def record(result: OperationResult) -> None:
            journal.add(result)

        executor = RedesignExecutor(guild, actor=str(interaction.user), admin_role_name=self.bot.settings.admin_role_name, on_result=record)
        executor.seed(plan)
        try:
            results = await executor.apply(operations)
        except BaseException:
            journal.finish("interrompue")
            raise
        if deletions:
            results += self._forget_deleted(results)
        else:
            results += self._save_configuration(plan, results, executor)
        for result in results[len(journal.data.get("results", [])):]:
            journal.add(result)
        remaining: tuple[str, ...] = ()
        verified = False
        if kind == REFONTE and not deletions and session is not None:
            await asyncio.sleep(self.settle_delay)
            fresh, _, _ = self.analyse(guild, session.options, interaction.user)
            remaining = tuple(describe(op) for op in fresh.actions if op.kind is not OpKind.BIND)
            verified = True
        manual = () if deletions else tuple(plan.of(OpKind.MANUAL))
        outcome = RedesignOutcome(tuple(results), manual, backup=backup, journal=journal.name, remaining=remaining,
                                  verified=verified, title=title)
        done = len(outcome.with_status(ResultStatus.DONE))
        failed = len(outcome.with_status(ResultStatus.FAILED))
        journal.finish("terminee" if not failed else "partielle", remaining=list(remaining))
        self.last_journal = journal.data
        self.bot.record_admin_action(interaction, action, plan.fingerprint, f"{done} réussie(s), {failed} échouée(s)")
        return outcome

    async def confirm_deletions(self, interaction: discord.Interaction, view: RedesignView) -> None:
        session = view.session
        if not await self._begin(interaction, session):
            return
        async with self.bot.redesign_lock:
            session.mode = RUNNING
            await interaction.response.edit_message(content=None, embed=running_embed("Revérification des éléments, puis suppression…"), view=None)
            guild = self._guild(interaction)
            plan, analysis, context = self._reanalyse(interaction, session)
            if plan.deletion_fingerprint != session.plan.deletion_fingerprint or not plan.can_delete:
                session.plan, session.context = plan, context
                session.analysis = analysis or session.analysis
                session.mode, session.page = DELETIONS, 0
                session.notice = "**La liste a changé depuis l'examen** : rien n'a été supprimé. Vérifiez la nouvelle liste."
                await view.show(interaction)
                return
            outcome = await self.apply(guild, plan, interaction, session, deletions=True)
            session.applied = True
            session.outcome, session.outcome_extra = outcome, []
            session.plan = RedesignPlan(plan.operations, plan.options, plan.blockers, plan.risks, (), plan.target, plan.report, plan.roles)
            session.mode, session.page, session.notice = DONE, 0, ""
            await view.show(interaction)

    def _save_configuration(self, plan: RedesignPlan, results: list[OperationResult], executor: RedesignExecutor) -> list[OperationResult]:
        """Enregistre les associations « fonction → salon / catégorie / rôle » d'après les identifiants."""
        saved = []
        restore = next((op for op in plan.of(OpKind.BIND) if "bindings" in op.params), None)
        if restore is not None:  # retour arrière : associations d'avant la refonte
            self.bot.bindings_repo.replace_all(restore.params["bindings"])
            return [OperationResult(restore, ResultStatus.DONE, "rétablies")]
        created = {r.operation.key: r.new_id for r in results if r.operation.kind is OpKind.CREATE_CHANNEL and r.new_id}
        bindings = dict(plan.report.bindings) if plan.report else {}
        bindings.update(created)
        self.bot.bindings_repo.replace_all(bindings)
        self.bot.save_category_bindings(executor.categories)
        self.bot.save_role_bindings(executor.roles)
        for op in plan.of(OpKind.BIND):
            if op.key in bindings:
                saved.append(OperationResult(op, ResultStatus.DONE, "enregistrée"))
            elif op.object_id is None and op.current:
                saved.append(OperationResult(op, ResultStatus.DONE, "ancienne association retirée"))
            else:
                saved.append(OperationResult(op, ResultStatus.SKIPPED, "salon non créé : association non enregistrée"))
        return saved

    def _forget_deleted(self, results: list[OperationResult]) -> list[OperationResult]:
        deleted = {r.operation.object_id for r in results if r.status is ResultStatus.DONE}
        bindings = self.bot.bindings_repo.get_all()
        kept = {k: v for k, v in bindings.items() if v not in deleted}
        if kept != bindings:
            self.bot.bindings_repo.replace_all(kept)
        roles = {k: v for k, v in self.bot.role_bindings.items() if v not in deleted}
        self.bot.save_role_bindings(roles)
        return []

    # --- Journal ---------------------------------------------------------------------------------

    def _start_journal(self, label: str, guild: discord.Guild, plan: RedesignPlan, interaction: discord.Interaction,
                       backup: str | None, session: RedesignSession | None) -> RedesignJournal:
        now = dt.datetime.now(dt.UTC)
        path = self.journal_dir / f"{label}-{now:%Y%m%d-%H%M%S}.json" if self.journal_dir else None
        snapshot = snapshot_guild(guild)
        data = {
            "version": 2,
            "kind": label,
            "status": "en_cours",
            "at": now.isoformat(timespec="seconds"),
            "user": {"id": interaction.user.id, "name": str(interaction.user)},
            "plan": plan.fingerprint,
            "options": asdict(plan.options),
            "backup": backup,
            "source": (session.journal or {}).get("name") if session and session.kind == RETOUR else None,
            # État complet avant application : identifiants, noms, types, catégories, positions, couleurs, permissions.
            "before": {
                "snapshot": asdict(snapshot),
                "bindings": self.bot.bindings_repo.get_all(),
                "categories": self.bot.category_bindings,
                "roles": self.bot.role_bindings,
            },
            "planned": [asdict(op) for op in (plan.deletions if label == "suppressions" else plan.actions)],
            "results": [],
        }
        journal = RedesignJournal(path, data)
        journal.data["name"] = journal.name
        journal.save()
        return journal

    def journal_names(self) -> list[str]:
        if self.journal_dir is None or not self.journal_dir.is_dir():
            return [self.last_journal["name"]] if self.last_journal and self.last_journal.get("name") else []
        return sorted((p.name for p in self.journal_dir.glob("*.json")), key=lambda n: n.split("-", 1)[-1], reverse=True)

    def load_journal(self, name: str | None = None) -> dict | None:
        if self.journal_dir is None:
            return self.last_journal
        names = self.journal_names()
        if name is None:
            name = next((n for n in names if not n.startswith(RETOUR)), None)
        if name is None or name not in names:
            return None
        try:
            return json.loads((self.journal_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Journal de refonte %s illisible : %s", name, exc)
            return None

    def _journal_context(self, data: dict) -> list[str]:
        results = data.get("results", [])
        done = sum(1 for r in results if r.get("status") == "done")
        return [
            f"Journal : `{data.get('name') or '?'}` · {data.get('kind', 'refonte')} du {data.get('at', '?')} par "
            f"{data.get('user', {}).get('name', '?')} · état : {data.get('status', '?')}",
            f"{done} opération(s) réussie(s) enregistrée(s) ; sauvegarde de la base : {data.get('backup') or 'aucune'}",
        ]

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


def _actor(guild: discord.Guild, user: discord.abc.User | None) -> Actor:
    if not isinstance(user, discord.Member):
        return Actor()
    return Actor(
        permissions=permission_names(user.guild_permissions),
        top_role_position=user.top_role.position if user.top_role is not None else 0,
        is_owner=guild.owner_id == user.id,
    )


def _differences(before: RedesignPlan, after: RedesignPlan, limit: int = 6) -> list[str]:
    old = {describe(op) for op in before.actions}
    new = {describe(op) for op in after.actions}
    lines = [f"nouveau : {line}" for line in sorted(new - old)] + [f"plus nécessaire : {line}" for line in sorted(old - new)]
    if old == new and before.blockers != after.blockers:
        lines.append("points bloquants modifiés")
    return lines[:limit] + ([f"… et {len(lines) - limit} autre(s)"] if len(lines) > limit else [])


async def setup(bot: SaeBot) -> None:
    await bot.add_cog(RedesignCog(bot))
