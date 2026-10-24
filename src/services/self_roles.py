"""Choix des rôles par les membres eux-mêmes (/roles, bouton « Choisir mes rôles »).

Règles de sécurité, appliquées ici sans dépendre de discord.py :

* seuls les rôles marqués ``self_assignable`` dans ``config/server.toml`` sont
  proposés (``config`` refuse déjà ce marqueur sur un rôle de staff, de
  responsabilité ou porteur de permissions) ;
* le rôle doit exister sur le serveur, ne pas être géré par une intégration,
  être placé sous le rôle de Samae et ne porter **aucune** permission sensible
  (un administrateur a pu modifier le rôle dans Discord après coup) ;
* un changement ne touche jamais un rôle hors de cette liste : les rôles
  attribués par l'administration restent tels quels.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.config import FORBIDDEN_PERMISSIONS, PERMISSION_LABELS, SENSITIVE_PERMISSIONS, RoleSpec, ServerLayout
from src.services.server_redesign import resolve_roles
from src.services.server_structure import GuildRole, GuildSnapshot

# Titres des sections du panneau et du menu, par type de rôle.
KIND_TITLES = {"fonction": "Domaines de travail", "notification": "Notifications"}
# Identifiant fixe du bouton « Choisir mes rôles » (messages permanents, message d'accueil).
ROLE_PICKER_ID = "samae:roles:open"
# Discord accepte au plus 25 options dans un menu de sélection.
MAX_OPTIONS = 25


@dataclass(frozen=True)
class SelfRole:
    key: str
    role_id: int
    name: str  # nom réel du rôle sur Discord
    description: str
    kind: str


@dataclass(frozen=True)
class SelfRoleProblem:
    key: str
    name: str  # nom attendu (config/server.toml)
    reason: str


@dataclass(frozen=True)
class SelfRoleCatalog:
    roles: tuple[SelfRole, ...] = ()
    problems: tuple[SelfRoleProblem, ...] = ()
    bot_can_manage: bool = True  # le bot a « Gérer les rôles » sur le serveur

    def by_id(self) -> dict[int, SelfRole]:
        return {role.role_id: role for role in self.roles}

    def grouped(self) -> list[tuple[str, list[SelfRole]]]:
        """Rôles regroupés par type, dans l'ordre de config/server.toml."""
        groups: dict[str, list[SelfRole]] = {}
        for role in self.roles:
            groups.setdefault(role.kind, []).append(role)
        return [(KIND_TITLES.get(kind, kind), items) for kind, items in groups.items()]


def build_catalog(layout: ServerLayout, snapshot: GuildSnapshot, saved: dict[str, int] | None = None) -> SelfRoleCatalog:
    """Rôles réellement proposés aux membres, et ceux écartés avec leur raison (pour /admin diagnostic)."""
    wanted = [spec for spec in layout.roles.values() if spec.self_assignable]
    if not wanted:
        return SelfRoleCatalog(bot_can_manage="manage_roles" in snapshot.bot_permissions)
    resolutions = {res.spec.key: res for res in resolve_roles(layout, snapshot, saved)}
    roles: list[SelfRole] = []
    problems: list[SelfRoleProblem] = []
    for spec in wanted:
        res = resolutions.get(spec.key)
        role = res.role if res else None
        if role is None:
            reason = res.note if res and res.note else "rôle introuvable"
            if res and not res.ambiguous and spec.optional:
                reason = "rôle facultatif, pas encore créé"
            problems.append(SelfRoleProblem(spec.key, spec.name, reason))
            continue
        reason = refusal_reason(role, snapshot)
        if reason:
            problems.append(SelfRoleProblem(spec.key, role.name, reason))
            continue
        if len(roles) >= MAX_OPTIONS:
            problems.append(SelfRoleProblem(spec.key, role.name, f"plus de {MAX_OPTIONS} rôles proposés (limite de Discord)"))
            continue
        roles.append(SelfRole(spec.key, role.id, role.name, _description(spec), spec.kind))
    return SelfRoleCatalog(tuple(roles), tuple(problems), "manage_roles" in snapshot.bot_permissions)


def refusal_reason(role: GuildRole, snapshot: GuildSnapshot) -> str:
    """Raison pour laquelle un rôle ne peut pas être choisi par un membre ; vide s'il peut l'être."""
    if role.is_default or role.managed:
        return "rôle réservé à Discord ou à une intégration"
    sensitive = sorted((set(role.permissions) | set(role.dangerous)) & (SENSITIVE_PERMISSIONS | FORBIDDEN_PERMISSIONS))
    if sensitive:
        labels = ", ".join(f"« {PERMISSION_LABELS.get(name, 'Administrateur' if name == 'administrator' else name)} »" for name in sensitive)
        return f"porte des permissions sensibles ({labels}) : retirez-les du rôle pour qu'il puisse être choisi"
    if snapshot.bot_top_role_position and role.position >= snapshot.bot_top_role_position:
        return "placé au-dessus du rôle de Samae : déplacez le rôle de Samae plus haut"
    return ""


def _description(spec: RoleSpec) -> str:
    return spec.description or "Aucune description."


@dataclass(frozen=True)
class RoleChange:
    add: tuple[SelfRole, ...] = ()
    remove: tuple[SelfRole, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.add and not self.remove


def plan_change(catalog: SelfRoleCatalog, current_role_ids: Iterable[int], selected_ids: Iterable[int]) -> RoleChange:
    """Rôles à ajouter et à retirer : seuls les rôles du catalogue sont pris en compte.

    Une valeur reçue qui n'est pas dans le catalogue (menu périmé, requête forgée) est ignorée.
    """
    offered = catalog.by_id()
    current = set(current_role_ids)
    selected = {role_id for role_id in selected_ids if role_id in offered}
    add = tuple(role for role_id, role in offered.items() if role_id in selected and role_id not in current)
    remove = tuple(role for role_id, role in offered.items() if role_id not in selected and role_id in current)
    return RoleChange(add, remove)


def describe_change(change: RoleChange) -> str:
    if change.empty:
        return "Aucun changement : vos rôles étaient déjà à jour."
    lines = []
    if change.add:
        lines.append("Ajouté(s) : " + ", ".join(f"**{role.name}**" for role in change.add))
    if change.remove:
        lines.append("Retiré(s) : " + ", ".join(f"**{role.name}**" for role in change.remove))
    return "\n".join(lines)
