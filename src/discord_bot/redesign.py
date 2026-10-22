"""Application d'un plan de refonte sur un vrai serveur Discord (/refonte).

Le plan vient de ``services.server_redesign`` et a été revalidé juste avant
l'appel. Ce module exécute ses opérations une par une, dans l'ordre du plan
(rôles, catégories, salons, permissions, positions), et rien d'autre. Chaque
opération :

* revérifie l'élément visé (toujours là, pas modifié entre-temps, hiérarchie) ;
* relit son état juste avant de le modifier (``undo``, pour le retour arrière) ;
* vérifie la réponse de Discord quand elle en renvoie une ;
* a son propre résultat : une erreur n'interrompt pas les opérations suivantes.

Les limites de débit de Discord (erreurs 429) sont gérées par discord.py, qui
attend puis réessaie ; un délai maximal par opération évite qu'une attente
bloque la refonte indéfiniment.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

from src.discord_bot.snapshot import channel_type, is_category, permission_names
from src.services.server_redesign import (
    Operation,
    OperationResult,
    OpKind,
    RedesignPlan,
    ResultStatus,
)
from src.services.server_structure import TYPE_FAMILY

log = logging.getLogger(__name__)

# Délai maximal d'une opération Discord (attente des limites de débit comprise).
OPERATION_TIMEOUT = 60.0
AUDIT_REASON = "Refonte Samae confirmée par {user}"


class RedesignExecutor:
    def __init__(
        self,
        guild: discord.Guild,
        *,
        actor: str,
        admin_role_name: str | None = None,
        timeout: float = OPERATION_TIMEOUT,
        on_result: Callable[[OperationResult], Awaitable[None]] | None = None,
    ) -> None:
        self.guild = guild
        # Visible dans le journal d'audit du serveur (Paramètres > Journal d'audit).
        self.reason = AUDIT_REASON.format(user=actor)[:512]
        self.admin_role_name = admin_role_name
        self.timeout = timeout
        self.on_result = on_result
        # Clé de la cible → identifiant Discord (existant ou créé pendant la refonte).
        self.categories: dict[str, int] = {}
        self.channels: dict[str, int] = {}
        self.roles: dict[str, int] = {}
        self.failed: set[str] = set()  # références (« cat:clé »…) dont la création a échoué

    def seed(self, plan: RedesignPlan) -> None:
        if plan.report is not None:
            self.categories.update(plan.report.category_bindings)
            self.channels.update(plan.report.bindings)
        self.roles.update({res.spec.key: res.role.id for res in plan.roles if res.role is not None})

    async def apply(self, operations: list[Operation]) -> list[OperationResult]:
        """Exécute les opérations Discord (la configuration de Samae est enregistrée ensuite par le cog)."""
        results: list[OperationResult] = []
        for op in operations:
            if op.kind is OpKind.BIND:
                continue
            result = await self._run(op)
            results.append(result)
            if self.on_result is not None:
                await self.on_result(result)
        return results

    async def _run(self, op: Operation) -> OperationResult:
        handler = {
            OpKind.CREATE_ROLE: self._create_role,
            OpKind.EDIT_ROLE: self._edit_role,
            OpKind.REORDER_ROLES: self._reorder_roles,
            OpKind.CREATE_CATEGORY: self._create_category,
            OpKind.EDIT_CATEGORY: self._edit_channel,
            OpKind.CREATE_CHANNEL: self._create_channel,
            OpKind.EDIT_CHANNEL: self._edit_channel,
            OpKind.MOVE_CHANNEL: self._move_channel,
            OpKind.SET_PERMISSIONS: self._set_permissions,
            OpKind.REORDER_CATEGORIES: self._reorder_categories,
            OpKind.REORDER_CHANNELS: self._reorder_channels,
            OpKind.DELETE_CHANNEL: self._delete,
            OpKind.DELETE_CATEGORY: self._delete,
            OpKind.DELETE_ROLE: self._delete_role,
        }[op.kind]
        try:
            result = await asyncio.wait_for(handler(op), timeout=self.timeout)
        except asyncio.TimeoutError:
            log.warning("Refonte : délai dépassé pour %s %s", op.kind, op.target)
            result = OperationResult(op, ResultStatus.FAILED, "délai dépassé : l'opération a pu aboutir quand même, relancez l'analyse pour le vérifier")
        except discord.Forbidden:
            log.warning("Refonte : permission refusée pour %s %s", op.kind, op.target)
            result = OperationResult(op, ResultStatus.FAILED, "permission refusée par Discord (rôle de Samae trop bas ou permission retirée)")
        except discord.HTTPException as exc:
            log.warning("Refonte : erreur Discord %s pour %s %s", exc.status, op.kind, op.target)
            result = OperationResult(op, ResultStatus.FAILED, f"erreur Discord {exc.status} : réessayez plus tard")
        except (ValueError, TypeError) as exc:
            log.exception("Refonte : opération %s %s invalide", op.kind, op.target)
            result = OperationResult(op, ResultStatus.FAILED, f"opération refusée ({exc.__class__.__name__})")
        if result.status is not ResultStatus.DONE and op.kind in (OpKind.CREATE_CATEGORY, OpKind.CREATE_CHANNEL, OpKind.CREATE_ROLE):
            prefix = {OpKind.CREATE_CATEGORY: "cat", OpKind.CREATE_CHANNEL: "chan", OpKind.CREATE_ROLE: "role"}[op.kind]
            self.failed.add(f"{prefix}:{op.key}")
        return result

    # --- Références ------------------------------------------------------------------

    def _ref_id(self, ref: str) -> int | None:
        prefix, _, value = ref.partition(":")
        if prefix == "id":
            return int(value)
        return {"cat": self.categories, "chan": self.channels, "role": self.roles}.get(prefix, {}).get(value)

    def _admin_role(self) -> discord.Role | None:
        if not self.admin_role_name:
            return None
        wanted = self.admin_role_name.casefold()
        return next((r for r in self.guild.roles if r.name.casefold() == wanted and not r.is_default()), None)

    def _target(self, key: str):
        """Objet Discord visé par une permission : @everyone, Samae, rôle d'administration, rôle de la cible ou identifiant."""
        if key == "@everyone":
            return self.guild.default_role
        if key == "@bot":
            return self.guild.me
        if key == "@admin":
            return self._admin_role()
        prefix, _, value = key.partition(":")
        if prefix == "role":
            role_id = self.roles.get(value)
            return self.guild.get_role(role_id) if role_id else None
        if prefix == "rid":
            return self.guild.get_role(int(value))
        if prefix == "mid":
            return self.guild.get_member(int(value)) or discord.Object(int(value), type=discord.Member)
        return None

    def _overwrites(self, spec: dict) -> dict:
        result = {}
        for key, rule in spec.items():
            target = self._target(key)
            if target is None or not (rule.get("allow") or rule.get("deny")):
                continue
            result[target] = _permission_overwrite(rule)
        return result

    # --- Rôles -------------------------------------------------------------------------

    async def _create_role(self, op: Operation) -> OperationResult:
        p = op.params
        wanted = p["name"].casefold()
        existing = next((r for r in self.guild.roles if r.name.casefold() == wanted), None)
        if existing is not None:
            self.roles[op.key] = existing.id
            return OperationResult(op, ResultStatus.SKIPPED, "un rôle de ce nom existe déjà : réutilisé")
        role = await self.guild.create_role(
            name=p["name"],
            permissions=discord.Permissions(**{name: True for name in p.get("permissions", [])}),
            colour=discord.Colour(p["color"]) if p.get("color") is not None else discord.Colour.default(),
            hoist=bool(p.get("hoist")),
            mentionable=bool(p.get("mentionable")),
            reason=self.reason,
        )
        self.roles[op.key] = role.id
        if role.name != p["name"]:
            return OperationResult(op, ResultStatus.FAILED, f"Discord a créé « {role.name} » au lieu de « {p['name']} »", new_id=role.id,
                                   undo={"created": role.id})
        return OperationResult(op, ResultStatus.DONE, "créé", new_id=role.id, undo={"created": role.id})

    def _role_problem(self, role: discord.Role | None) -> str:
        if role is None:
            return "introuvable : supprimé depuis l'analyse ?"
        me = self.guild.me
        if role.managed or (me is not None and role.position >= me.top_role.position):
            return "au-dessus du rôle de Samae (hiérarchie Discord) : non modifié"
        return ""

    async def _edit_role(self, op: Operation) -> OperationResult:
        role = self.guild.get_role(op.object_id or 0)
        problem = self._role_problem(role)
        if problem:
            return OperationResult(op, ResultStatus.FAILED if role is None else ResultStatus.SKIPPED, problem)
        if role.name != op.current:
            return OperationResult(op, ResultStatus.SKIPPED, f"renommé entre-temps en « {role.name} » : non modifié")
        p = op.params
        undo = {"id": role.id, "name": role.name, "color": role.colour.value, "hoist": role.hoist,
                "mentionable": role.mentionable, "permissions": sorted(permission_names(role.permissions))}
        kwargs: dict = {"reason": self.reason}
        if "name" in p:
            kwargs["name"] = p["name"]
        if "color" in p:
            kwargs["colour"] = discord.Colour(p["color"])
        for flag in ("hoist", "mentionable"):
            if flag in p:
                kwargs[flag] = p[flag]
        if p.get("add_permissions") or p.get("remove_permissions"):
            permissions = discord.Permissions(role.permissions.value)
            permissions.update(**{n: True for n in p.get("add_permissions", [])}, **{n: False for n in p.get("remove_permissions", [])})
            kwargs["permissions"] = permissions
        if "set_permissions" in p:
            kwargs["permissions"] = discord.Permissions(**{n: True for n in p["set_permissions"]})
        edited = await role.edit(**kwargs)
        if edited is not None and "name" in p and edited.name != p["name"]:
            return OperationResult(op, ResultStatus.FAILED, "Discord n'a pas appliqué le nouveau nom", undo=undo)
        return OperationResult(op, ResultStatus.DONE, "; ".join(op.changes) or "modifié", undo=undo)

    async def _reorder_roles(self, op: Operation) -> OperationResult:
        if "positions" in op.params:
            return await self._restore_positions(op)
        me = self.guild.me
        roles = []
        for ref in op.params["order"]:
            role_id = self._ref_id(ref)
            role = self.guild.get_role(role_id) if role_id else None
            if role is not None and not role.managed and (me is None or role.position < me.top_role.position):
                roles.append(role)
        if len(roles) < 2:
            return OperationResult(op, ResultStatus.SKIPPED, "moins de deux rôles à ordonner")
        if [r.id for r in sorted(roles, key=lambda r: -r.position)] == [r.id for r in roles]:
            return OperationResult(op, ResultStatus.SKIPPED, "déjà dans l'ordre")
        ceiling = (me.top_role.position - 1) if me is not None else max(r.position for r in roles)
        top = max(min(max(r.position for r in roles), ceiling), len(roles))
        positions = {role: max(1, top - index) for index, role in enumerate(roles)}
        undo = {"positions": {str(r.id): r.position for r in roles}}
        await self.guild.edit_role_positions(positions=positions, reason=self.reason)
        return OperationResult(op, ResultStatus.DONE, f"{len(roles)} rôles ordonnés", undo=undo)

    async def _delete_role(self, op: Operation) -> OperationResult:
        role = self.guild.get_role(op.params["id"])
        problem = self._role_problem(role)
        if problem:
            return OperationResult(op, ResultStatus.SKIPPED, problem)
        if role.name != op.params["name"]:
            return OperationResult(op, ResultStatus.SKIPPED, f"renommé entre-temps en « {role.name} » : conservé")
        undo = {"deleted": "role", "name": role.name, "color": role.colour.value, "hoist": role.hoist,
                "mentionable": role.mentionable, "permissions": sorted(permission_names(role.permissions)), "position": role.position}
        await role.delete(reason=self.reason)
        return OperationResult(op, ResultStatus.DONE, "supprimé (retiré aux membres qui l'avaient)", undo=undo)

    # --- Catégories et salons --------------------------------------------------------------

    async def _create_category(self, op: Operation) -> OperationResult:
        p = op.params
        category = await self.guild.create_category(p["name"], overwrites=self._overwrites(p.get("overwrites", {})), reason=self.reason)
        self.categories[op.key] = category.id
        return OperationResult(op, ResultStatus.DONE, "créée", new_id=category.id, undo={"created": category.id})

    async def _create_channel(self, op: Operation) -> OperationResult:
        p = op.params
        if p["category"] in self.failed:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie non créée : salon non créé")
        category = None
        if p.get("category"):  # vide : salon recréé hors catégorie (retour arrière)
            category_id = self._ref_id(p["category"])
            category = self.guild.get_channel(category_id) if category_id else None
            if category is None:
                return OperationResult(op, ResultStatus.SKIPPED, "catégorie introuvable : salon non créé")
        kwargs = {"category": category, "overwrites": self._overwrites(p.get("overwrites", {})), "reason": self.reason}
        kind = p["type"]
        if kind in ("text", "announcement"):
            channel = await self.guild.create_text_channel(p["name"], topic=p.get("topic") or "", news=kind == "announcement", **kwargs)
        elif kind == "voice":
            channel = await self.guild.create_voice_channel(p["name"], **kwargs)
        elif kind == "stage":
            channel = await self.guild.create_stage_channel(p["name"], **kwargs)
        else:
            channel = await self.guild.create_forum(p["name"], topic=p.get("topic") or "", media=kind == "media", **kwargs)
        self.channels[op.key] = channel.id
        return OperationResult(op, ResultStatus.DONE, "créé", new_id=channel.id, undo={"created": channel.id})

    async def _edit_channel(self, op: Operation) -> OperationResult:
        channel = self.guild.get_channel(op.object_id or 0)
        if channel is None:
            return OperationResult(op, ResultStatus.FAILED, "introuvable : supprimé depuis l'analyse ?")
        if channel.name != op.current:
            if channel.name == op.params.get("name"):
                return OperationResult(op, ResultStatus.SKIPPED, "déjà renommé")
            return OperationResult(op, ResultStatus.SKIPPED, f"renommé entre-temps en « {channel.name} » : non modifié")
        p = op.params
        undo = {"id": channel.id, "name": channel.name}
        kwargs: dict = {"reason": self.reason}
        if "name" in p:
            kwargs["name"] = p["name"]
        if "topic" in p:
            undo["topic"] = getattr(channel, "topic", None) or ""
            kwargs["topic"] = p["topic"]
        if "type" in p:
            undo["type"] = channel_type(channel)
            kwargs["type"] = discord.ChannelType.news if p["type"] == "announcement" else discord.ChannelType.text
        edited = await channel.edit(**kwargs)
        if edited is not None and "name" in p and edited.name != p["name"]:
            return OperationResult(op, ResultStatus.FAILED, f"Discord a enregistré « {edited.name} »", undo=undo)
        return OperationResult(op, ResultStatus.DONE, "; ".join(op.changes) or "modifié", undo=undo)

    async def _move_channel(self, op: Operation) -> OperationResult:
        if op.params["category"] in self.failed:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie non créée : salon laissé à sa place")
        channel = self.guild.get_channel(op.object_id or 0)
        if channel is None:
            return OperationResult(op, ResultStatus.FAILED, "introuvable : supprimé depuis l'analyse ?")
        category_id = self._ref_id(op.params["category"])
        category = self.guild.get_channel(category_id) if category_id else None
        if category is None:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie cible introuvable : salon laissé à sa place")
        if channel.category_id == category.id:
            return OperationResult(op, ResultStatus.SKIPPED, "déjà dans la bonne catégorie")
        undo = {"id": channel.id, "category_id": channel.category_id, "position": channel.position}
        # sync_permissions=False : le salon garde ses permissions ; la correction éventuelle vient ensuite.
        await channel.edit(category=category, sync_permissions=False, reason=self.reason)
        return OperationResult(op, ResultStatus.DONE, f"rangé dans « {category.name} »", undo=undo)

    async def _set_permissions(self, op: Operation) -> OperationResult:
        channel = self.guild.get_channel(op.object_id or 0)
        if channel is None:
            return OperationResult(op, ResultStatus.FAILED, "introuvable : supprimé depuis l'analyse ?")
        overwrites = dict(channel.overwrites)
        undo: dict[str, dict | None] = {}
        skipped = []
        for key, rule in op.params["targets"].items():
            target = self._target(key)
            if target is None:
                skipped.append(key)
                continue
            ref = f"{'rid' if self.guild.get_role(target.id) is not None else 'mid'}:{target.id}"
            current = overwrites.get(target)
            undo[ref] = _serialize_overwrite(current) if current is not None and not current.is_empty() else None
            if rule.get("allow") or rule.get("deny"):
                overwrites[target] = _permission_overwrite(rule)
            else:
                overwrites.pop(target, None)
        await channel.edit(overwrites=overwrites, reason=self.reason)
        detail = "; ".join(op.changes)
        if skipped:
            detail += f" (cibles absentes ignorées : {', '.join(skipped)})"
        return OperationResult(op, ResultStatus.DONE, detail, undo={"id": channel.id, "overwrites": undo})

    async def _bulk_positions(self, payload: list[dict]) -> None:
        # discord.py n'expose pas de méthode publique pour déplacer plusieurs salons en un appel ;
        # l'appel groupé évite de dépendre du cache pendant une série de déplacements.
        await self.guild._state.http.bulk_channel_update(self.guild.id, payload, reason=self.reason)

    async def _restore_positions(self, op: Operation) -> OperationResult:
        """Retour arrière : positions relevées avant la refonte, pour les éléments qui existent encore."""
        positions = {int(k): v for k, v in op.params["positions"].items()}
        if op.kind is OpKind.REORDER_ROLES:
            me = self.guild.me
            roles = {r: positions[r.id] for r in self.guild.roles
                     if r.id in positions and not r.managed and (me is None or r.position < me.top_role.position)
                     and r.position != positions[r.id]}
            if not roles:
                return OperationResult(op, ResultStatus.SKIPPED, "déjà à leur place")
            await self.guild.edit_role_positions(positions=roles, reason=self.reason)
            return OperationResult(op, ResultStatus.DONE, f"{len(roles)} position(s) rétablie(s)")
        payload = [{"id": cid, "position": pos} for cid, pos in sorted(positions.items(), key=lambda item: item[1])
                   if self.guild.get_channel(cid) is not None]
        if not payload:
            return OperationResult(op, ResultStatus.SKIPPED, "éléments introuvables")
        await self._bulk_positions(payload)
        return OperationResult(op, ResultStatus.DONE, f"{len(payload)} position(s) rétablie(s)")

    async def _reorder_categories(self, op: Operation) -> OperationResult:
        if "positions" in op.params:
            return await self._restore_positions(op)
        ids = []
        for ref in op.params["order"]:
            category_id = self._ref_id(ref)
            if category_id is not None and is_category(self.guild.get_channel(category_id)):
                ids.append(category_id)
        live = [c.id for c in sorted(self.guild.categories, key=lambda c: (c.position, c.id))]
        ids += [c for c in live if c not in ids]
        if ids == live:
            return OperationResult(op, ResultStatus.SKIPPED, "déjà dans l'ordre")
        undo = {"positions": {str(c.id): c.position for c in self.guild.categories}}
        await self._bulk_positions([{"id": category_id, "position": index} for index, category_id in enumerate(ids)])
        return OperationResult(op, ResultStatus.DONE, f"{len(ids)} catégories ordonnées", undo=undo)

    async def _reorder_channels(self, op: Operation) -> OperationResult:
        if "positions" in op.params:
            return await self._restore_positions(op)
        p = op.params
        if p["category"] in self.failed:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie non créée")
        category_id = self._ref_id(p["category"])
        if category_id is None or self.guild.get_channel(category_id) is None:
            return OperationResult(op, ResultStatus.SKIPPED, "catégorie introuvable")
        channels = []
        for ref in p["order"]:
            channel_id = self._ref_id(ref)
            channel = self.guild.get_channel(channel_id) if channel_id else None
            if channel is not None and not is_category(channel):
                channels.append(channel)
        if not channels:
            return OperationResult(op, ResultStatus.SKIPPED, "aucun salon à ordonner")
        undo = {"positions": {str(c.id): c.position for c in channels}}
        payload = [
            {"id": channel.id, "position": index, "parent_id": category_id, "lock_permissions": False}
            for index, channel in enumerate(sorted(channels, key=lambda item: (TYPE_FAMILY.get(channel_type(item)) == "voice", channels.index(item))))
        ]
        await self._bulk_positions(payload)
        return OperationResult(op, ResultStatus.DONE, f"{len(channels)} salons ordonnés", undo=undo)

    async def _delete(self, op: Operation) -> OperationResult:
        channel = self.guild.get_channel(op.params["id"])
        if channel is None:
            return OperationResult(op, ResultStatus.SKIPPED, "déjà absent")
        if channel.name != op.params["name"]:
            return OperationResult(op, ResultStatus.SKIPPED, f"renommé entre-temps en « {channel.name} » : conservé")
        if is_category(channel):
            if channel.channels:
                return OperationResult(op, ResultStatus.SKIPPED, "contient de nouveau des salons : conservée")
        elif op.params.get("require_empty") and getattr(channel, "last_message_id", None) is not None:
            return OperationResult(op, ResultStatus.SKIPPED, "contient désormais des messages : conservé")
        undo = {
            "deleted": "category" if is_category(channel) else channel_type(channel),
            "name": channel.name,
            "topic": getattr(channel, "topic", None) or "",
            "category_id": getattr(channel, "category_id", None),
            "position": channel.position,
            "overwrites": {
                f"{'rid' if self.guild.get_role(target.id) is not None else 'mid'}:{target.id}": _serialize_overwrite(ow)
                for target, ow in channel.overwrites.items()
            },
        }
        await channel.delete(reason=self.reason)
        return OperationResult(op, ResultStatus.DONE, "supprimé", undo=undo)


def _permission_overwrite(rule: dict) -> discord.PermissionOverwrite:
    values = {name: True for name in rule.get("allow", [])} | {name: False for name in rule.get("deny", [])}
    return discord.PermissionOverwrite(**values)


def _serialize_overwrite(overwrite: discord.PermissionOverwrite) -> dict:
    allow, deny = overwrite.pair()
    return {"allow": sorted(permission_names(allow)), "deny": sorted(permission_names(deny))}
