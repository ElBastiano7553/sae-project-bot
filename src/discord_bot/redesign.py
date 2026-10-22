"""Application d'un plan de refonte sur un vrai serveur Discord (/refonte).

Le plan vient de ``services.server_redesign`` et a déjà été revalidé juste avant
l'appel. Ce module exécute ses opérations une par une, dans l'ordre, et ne fait
**jamais** rien d'autre : aucune suppression, aucun rôle existant modifié, aucune
permission de salon existant touchée. Chaque opération a son propre résultat ;
une erreur n'interrompt pas les opérations indépendantes qui suivent.

Les limites de débit de Discord (erreurs 429) sont gérées par discord.py, qui
attend puis réessaie ; un délai maximal par opération évite qu'une attente
bloque la refonte indéfiniment.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from src.services.server_redesign import (
    Operation,
    OperationResult,
    OpKind,
    RedesignPlan,
    ResultStatus,
)

log = logging.getLogger(__name__)

# Délai maximal d'une opération Discord (attente des limites de débit comprise).
OPERATION_TIMEOUT = 60.0
AUDIT_REASON = "Refonte Samae confirmée par {user}"


class RedesignExecutor:
    def __init__(self, guild: discord.Guild, *, actor: str, admin_role_name: str | None = None, timeout: float = OPERATION_TIMEOUT) -> None:
        self.guild = guild
        # Visible dans le journal d'audit du serveur (Paramètres > Journal d'audit).
        self.reason = AUDIT_REASON.format(user=actor)[:512]
        self.admin_role_name = admin_role_name
        self.timeout = timeout
        self.categories: dict[str, int] = {}  # clé de catégorie → identifiant (existante ou créée)
        self.failed_categories: set[str] = set()

    async def apply(self, plan: RedesignPlan) -> list[OperationResult]:
        """Exécute les opérations de Discord (la configuration de Samae est enregistrée ensuite par le cog)."""
        if plan.report is not None:
            self.categories.update(plan.report.category_bindings)
        results: list[OperationResult] = []
        for op in plan.actions:
            if op.kind is OpKind.BIND:
                continue
            results.append(await self._run(op, plan))
        return results

    async def _run(self, op: Operation, plan: RedesignPlan) -> OperationResult:
        handler = {
            OpKind.CREATE_CATEGORY: self._create_category,
            OpKind.RENAME_CATEGORY: self._rename,
            OpKind.CREATE_CHANNEL: self._create_channel,
            OpKind.RENAME_CHANNEL: self._rename,
            OpKind.MOVE_CHANNEL: self._move_channel,
            OpKind.CREATE_ROLE: self._create_role,
        }[op.kind]
        try:
            result = await asyncio.wait_for(handler(op, plan), timeout=self.timeout)
        except asyncio.TimeoutError:
            log.warning("Refonte : délai dépassé pour %s %s", op.kind, op.target)
            result = OperationResult(op, ResultStatus.FAILED, "délai dépassé : l'opération a pu aboutir quand même, relancez l'analyse pour le vérifier")
        except discord.Forbidden:
            log.warning("Refonte : permission refusée pour %s %s", op.kind, op.target)
            result = OperationResult(op, ResultStatus.FAILED, "permission refusée par Discord (rôle du bot trop bas ou permission retirée)")
        except discord.HTTPException as exc:
            log.warning("Refonte : erreur Discord %s pour %s %s", exc.status, op.kind, op.target)
            result = OperationResult(op, ResultStatus.FAILED, f"erreur Discord {exc.status} : réessayez plus tard")
        if op.kind is OpKind.CREATE_CATEGORY and result.status is not ResultStatus.DONE:
            self.failed_categories.add(op.key)
        return result

    # --- Opérations ------------------------------------------------------------------

    def _private_overwrites(self) -> dict:
        """Catégorie privée : invisible pour @everyone, visible pour le bot et le rôle d'administration du bot."""
        overwrites = {self.guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        if self.guild.me is not None:
            overwrites[self.guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, read_message_history=True
            )
        if self.admin_role_name:
            wanted = self.admin_role_name.casefold()
            role = next((r for r in self.guild.roles if r.name.casefold() == wanted), None)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
        return overwrites

    async def _create_category(self, op: Operation, plan: RedesignPlan) -> OperationResult:
        kwargs = {"reason": self.reason}
        if op.private:
            kwargs["overwrites"] = self._private_overwrites()
        category = await self.guild.create_category(op.target, **kwargs)
        self.categories[op.key] = category.id
        note = await self._place_category(category, op, plan)
        return OperationResult(op, ResultStatus.DONE, "créée" + (" (privée)" if op.private else "") + note, new_id=category.id)

    async def _place_category(self, category, op: Operation, plan: RedesignPlan) -> str:
        """Range la nouvelle catégorie après la précédente de la structure cible (sinon : en bas de la liste)."""
        if plan.report is None:
            return ""
        keys = [res.spec.key for res in plan.report.categories]
        previous = None
        for key in reversed(keys[: keys.index(op.key)]):
            if key in self.categories:
                previous = self.guild.get_channel(self.categories[key])
                if previous is not None:
                    break
        if previous is None:
            return ""
        try:
            await category.move(after=previous, reason=self.reason)
        except discord.HTTPException:
            log.info("Refonte : catégorie %s créée mais non déplacée", op.target)
            return ", position à ajuster à la main"
        return ""

    async def _create_channel(self, op: Operation, plan: RedesignPlan) -> OperationResult:
        if op.category_key in self.failed_categories:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie non créée : salon non créé")
        category_id = self.categories.get(op.category_key or "")
        category = self.guild.get_channel(category_id) if category_id else None
        if category is None:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie introuvable : salon non créé")
        kwargs = {"category": category, "reason": self.reason}
        if op.private:
            kwargs["overwrites"] = self._private_overwrites()
        create = self.guild.create_voice_channel if op.subject == "voice" else self.guild.create_text_channel
        channel = await create(op.target, **kwargs)
        return OperationResult(op, ResultStatus.DONE, "créé" + (" (privé)" if op.private else ""), new_id=channel.id)

    async def _rename(self, op: Operation, plan: RedesignPlan) -> OperationResult:
        channel = self.guild.get_channel(op.object_id) if op.object_id else None
        if channel is None:
            return OperationResult(op, ResultStatus.FAILED, "introuvable : supprimé depuis l'analyse ?")
        if channel.name == op.target:
            return OperationResult(op, ResultStatus.SKIPPED, "déjà renommé")
        if channel.name != op.current:
            return OperationResult(op, ResultStatus.SKIPPED, f"renommé entre-temps en « {channel.name} » : non modifié")
        await channel.edit(name=op.target, reason=self.reason)
        return OperationResult(op, ResultStatus.DONE, f"« {op.current} » → « {op.target} »")

    async def _move_channel(self, op: Operation, plan: RedesignPlan) -> OperationResult:
        if op.category_key in self.failed_categories:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie non créée : salon laissé à sa place")
        channel = self.guild.get_channel(op.object_id) if op.object_id else None
        if channel is None:
            return OperationResult(op, ResultStatus.FAILED, "introuvable : supprimé depuis l'analyse ?")
        category_id = self.categories.get(op.category_key or "")
        category = self.guild.get_channel(category_id) if category_id else None
        if category is None:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie cible introuvable : salon laissé à sa place")
        if channel.category_id == category.id:
            return OperationResult(op, ResultStatus.SKIPPED, "déjà dans la bonne catégorie")
        # sync_permissions=False : le salon garde exactement ses permissions actuelles.
        await channel.edit(category=category, sync_permissions=False, reason=self.reason)
        return OperationResult(op, ResultStatus.DONE, f"rangé dans « {category.name} »")

    async def _create_role(self, op: Operation, plan: RedesignPlan) -> OperationResult:
        wanted = op.target.casefold()
        if any(role.name.casefold() == wanted for role in self.guild.roles):
            return OperationResult(op, ResultStatus.SKIPPED, "un rôle de ce nom existe déjà")
        role = await self.guild.create_role(
            name=op.target, permissions=discord.Permissions.none(), mentionable=False, hoist=False, reason=self.reason
        )
        return OperationResult(op, ResultStatus.DONE, "créé sans permission", new_id=role.id)
