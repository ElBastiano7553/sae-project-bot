"""Refonte du serveur (/refonte) : inventaire, comparaison à la cible et plan, sans discord.py.

Le plan est calculé uniquement à partir d'un instantané du serveur
(``GuildSnapshot``), de la structure cible (``config/server.toml`` vue à travers un
profil et un style, voir ``redesign_target``) et des associations déjà
enregistrées. Il est **déterministe** : le même état et la même cible donnent les
mêmes opérations, dans le même ordre, avec la même empreinte. L'empreinte permet
de vérifier, au moment de confirmer, que le serveur n'a pas changé depuis l'aperçu.

Chaque élément existant reçoit une décision explicite : conservé, renommé,
sujet ou type modifié, déplacé, permissions corrigées, réordonné, associé à une
fonction de Samae, signalé comme doublon, personnalisé ou de fonction inconnue.

Rien n'est jamais supprimé par le plan principal : les suppressions forment une
liste **séparée** (``RedesignPlan.deletions``), proposée seulement pour des cas
justifiés (catégorie vide hors structure, doublon vide, élément déclaré
obsolète), appliquée seulement avec le mode suppressions et une confirmation
distincte.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from src.config import PERMISSION_LABELS, SENSITIVE_PERMISSIONS, CategorySpec, ChannelSpec, RoleSpec, ServerLayout
from src.services.redesign_target import NEUTRAL, Rule, Target, build_target
from src.services.server_structure import (
    TYPE_FAMILY,
    ChannelStatus,
    GuildCategory,
    GuildChannel,
    GuildRole,
    GuildSnapshot,
    Overwrite,
    StructureReport,
    normalize_name,
    resolve_structure,
)

# Limites de Discord : 500 salons (catégories comprises) par serveur, 50 salons par catégorie, 250 rôles.
MAX_GUILD_CHANNELS = 500
MAX_CATEGORY_CHANNELS = 50
MAX_ROLES = 250
# Permissions sensibles relevées dans l'analyse des rôles.
DANGEROUS_PERMISSIONS = {
    "administrator": "Administrateur",
    "manage_guild": "Gérer le serveur",
    "manage_roles": "Gérer les rôles",
    "manage_channels": "Gérer les salons",
    "kick_members": "Expulser des membres",
    "ban_members": "Bannir des membres",
    "mention_everyone": "Mentionner @everyone",
    "manage_webhooks": "Gérer les webhooks",
}
BOT_PERMISSION_LABELS = {
    "manage_channels": "Gérer les salons",
    "manage_roles": "Gérer les rôles",
    "view_channel": "Voir les salons",
}
# Types créés à la place quand le serveur n'a pas la fonction « Communauté ».
COMMUNITY_FALLBACK = {"announcement": "text", "media": "text", "forum": "text", "stage": "voice"}
TYPE_LABELS = {
    "text": "textuel",
    "announcement": "d'annonces",
    "voice": "vocal",
    "stage": "de conférence",
    "forum": "forum",
    "media": "médias",
    "other": "autre",
}


class OpKind(StrEnum):
    CREATE_ROLE = "create_role"
    EDIT_ROLE = "edit_role"
    REORDER_ROLES = "reorder_roles"
    CREATE_CATEGORY = "create_category"
    EDIT_CATEGORY = "edit_category"
    CREATE_CHANNEL = "create_channel"
    EDIT_CHANNEL = "edit_channel"  # nom, sujet ou type (texte ↔ annonces)
    MOVE_CHANNEL = "move_channel"
    SET_PERMISSIONS = "set_permissions"
    REORDER_CATEGORIES = "reorder_categories"
    REORDER_CHANNELS = "reorder_channels"
    BIND = "bind"  # association « fonction de Samae → salon » enregistrée en base
    # Destructives : jamais dans le plan principal, toujours sur confirmation séparée.
    DELETE_CHANNEL = "delete_channel"
    DELETE_CATEGORY = "delete_category"
    DELETE_ROLE = "delete_role"
    # Informatives : aucune action.
    KEEP = "keep"  # reconnu et déjà conforme
    SKIP = "skip"  # volontairement non traité (facultatif, option non choisie…)
    MANUAL = "manual"  # impossible pour Samae : intervention humaine
    CUSTOM = "custom"  # élément personnalisé déclaré dans [refonte] keep : conservé tel quel
    UNKNOWN = "unknown"  # fonction inconnue : conservé, à vérifier
    DUPLICATE = "duplicate"  # doublon potentiel d'un élément de la cible


EXECUTION_ORDER = [
    OpKind.CREATE_ROLE,
    OpKind.EDIT_ROLE,
    OpKind.REORDER_ROLES,
    OpKind.CREATE_CATEGORY,
    OpKind.EDIT_CATEGORY,
    OpKind.CREATE_CHANNEL,
    OpKind.EDIT_CHANNEL,
    OpKind.MOVE_CHANNEL,
    OpKind.SET_PERMISSIONS,
    OpKind.REORDER_CATEGORIES,
    OpKind.REORDER_CHANNELS,
    OpKind.BIND,
]
DESTRUCTIVE = [OpKind.DELETE_CHANNEL, OpKind.DELETE_CATEGORY, OpKind.DELETE_ROLE]
ACTIONABLE = set(EXECUTION_ORDER)
KIND_LABELS = {
    OpKind.CREATE_ROLE: "Rôles à créer",
    OpKind.EDIT_ROLE: "Rôles existants à compléter",
    OpKind.REORDER_ROLES: "Hiérarchie des rôles",
    OpKind.CREATE_CATEGORY: "Catégories à créer",
    OpKind.EDIT_CATEGORY: "Catégories à renommer",
    OpKind.CREATE_CHANNEL: "Salons à créer",
    OpKind.EDIT_CHANNEL: "Salons à renommer ou modifier",
    OpKind.MOVE_CHANNEL: "Salons à déplacer",
    OpKind.SET_PERMISSIONS: "Permissions à corriger",
    OpKind.REORDER_CATEGORIES: "Ordre des catégories",
    OpKind.REORDER_CHANNELS: "Ordre des salons",
    OpKind.BIND: "Configuration de Samae",
    OpKind.DELETE_CHANNEL: "Salons à supprimer",
    OpKind.DELETE_CATEGORY: "Catégories à supprimer",
    OpKind.DELETE_ROLE: "Rôles à supprimer",
    OpKind.KEEP: "Conservés sans changement",
    OpKind.SKIP: "Ignorés",
    OpKind.MANUAL: "Impossible pour Samae : à faire à la main",
    OpKind.CUSTOM: "Éléments personnalisés (conservés tels quels)",
    OpKind.UNKNOWN: "Fonction inconnue (conservés, à vérifier)",
    OpKind.DUPLICATE: "Doublons potentiels (conservés)",
}


@dataclass(frozen=True)
class Operation:
    kind: OpKind
    key: str  # clé de config/server.toml, ou « id:<n> » pour un élément hors structure
    subject: str  # "role", "category", type de salon ("text", "voice"…) ou "config"
    target: str  # nom cible (ou nom actuel d'un élément hors structure)
    object_id: int | None = None  # identifiant Discord de l'élément existant
    current: str = ""  # nom actuel
    category_key: str | None = None  # catégorie cible (création ou déplacement)
    params: dict = field(default_factory=dict)  # données d'exécution : nouveaux attributs, permissions, ordre
    changes: tuple[str, ...] = ()  # détail lisible des modifications
    expected: str = ""  # résultat attendu, ou raison (ignoré, manuel)

    @property
    def actionable(self) -> bool:
        return self.kind in ACTIONABLE


@dataclass(frozen=True)
class RedesignOptions:
    profile: str = ""  # vide : profil de la section [refonte]
    style: str = ""  # vide : style du profil
    create_optional: bool = False  # créer aussi les salons et rôles facultatifs absents
    manage_roles: bool = True  # créer et compléter les rôles, ordonner la hiérarchie
    manage_permissions: bool = True  # corriger les permissions (si [refonte] permissions = true)
    deletions: bool = False  # mode suppressions : proposer la liste séparée, sur confirmation distincte


@dataclass(frozen=True)
class Actor:
    """La personne qui lance la refonte : Samae ne fait jamais pour elle ce qu'elle ne pourrait pas faire."""

    permissions: frozenset[str] = frozenset()
    top_role_position: int = 0
    is_owner: bool = False

    def can(self, permission: str) -> bool:
        return self.is_owner or "administrator" in self.permissions or permission in self.permissions

    def outranks(self, position: int) -> bool:
        return self.is_owner or position < self.top_role_position


OWNER = Actor(is_owner=True)


@dataclass(frozen=True)
class RoleResolution:
    spec: RoleSpec
    role: GuildRole | None
    match: str = ""  # "id", "name" ou "alias"
    note: str = ""
    ambiguous: bool = False


@dataclass(frozen=True)
class RedesignPlan:
    operations: tuple[Operation, ...]
    options: RedesignOptions
    blockers: tuple[str, ...] = ()  # empêchent l'application (permissions du bot, limites, verrouillage)
    risks: tuple[str, ...] = ()  # points d'attention, sans effet sur l'application
    deletions: tuple[Operation, ...] = ()  # suppressions proposées : séparées, jamais appliquées avec le reste
    target: Target | None = field(default=None, compare=False)
    report: StructureReport | None = field(default=None, compare=False)
    roles: tuple[RoleResolution, ...] = field(default=(), compare=False)

    def of(self, *kinds: OpKind) -> list[Operation]:
        return [op for op in (*self.operations, *self.deletions) if op.kind in kinds]

    @property
    def actions(self) -> list[Operation]:
        """Opérations non destructives à exécuter, dans l'ordre d'exécution."""
        return sorted((op for op in self.operations if op.actionable), key=lambda op: EXECUTION_ORDER.index(op.kind))

    @property
    def counts(self) -> dict[OpKind, int]:
        counter = Counter(op.kind for op in (*self.operations, *self.deletions))
        return {kind: counter.get(kind, 0) for kind in OpKind}

    @property
    def needs_changes(self) -> bool:
        return bool(self.actions)

    @property
    def can_apply(self) -> bool:
        return self.needs_changes and not self.blockers

    @property
    def can_delete(self) -> bool:
        return self.options.deletions and bool(self.deletions) and not self.blockers

    @property
    def fingerprint(self) -> str:
        """Empreinte des opérations à exécuter : change dès que le serveur ou la cible changent."""
        return _digest([asdict(op) for op in self.actions] + [asdict(self.options), list(self.blockers)])

    @property
    def deletion_fingerprint(self) -> str:
        return _digest([asdict(op) for op in self.deletions])


def _digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


# --- Rôles : reconnaissance des rôles existants -------------------------------------------------


def resolve_roles(layout: ServerLayout, snapshot: GuildSnapshot, saved: dict[str, int] | None = None) -> list[RoleResolution]:
    """Associe chaque rôle de la cible à un rôle existant : identifiant enregistré, nom, puis anciens noms."""
    saved = saved or {}
    candidates = [role for role in snapshot.roles if not role.is_default and not role.managed]
    by_id = {role.id: role for role in candidates}
    resolutions = []
    for spec in layout.roles.values():
        role_id = saved.get(spec.key)
        if role_id in by_id:
            resolutions.append(RoleResolution(spec, by_id[role_id], "id"))
            continue
        found = None
        for wanted, match in [(normalize_name(spec.name), "name")] + [(normalize_name(a), "alias") for a in spec.aliases]:
            matches = [role for role in candidates if normalize_name(role.name) == wanted]
            if len(matches) == 1:
                found = RoleResolution(spec, matches[0], match)
                break
            if len(matches) > 1:
                found = RoleResolution(spec, None, note=f"{len(matches)} rôles s'appellent « {matches[0].name} »", ambiguous=True)
                break
        resolutions.append(found or RoleResolution(spec, None, note="rôle introuvable"))
    # Un rôle existant ne sert qu'une fois : la correspondance la plus sûre l'emporte.
    strength = {"id": 0, "name": 1, "alias": 2}
    owner: dict[int, int] = {}
    for index, res in enumerate(resolutions):
        if res.role is not None:
            current = owner.get(res.role.id)
            if current is None or strength[res.match] < strength[resolutions[current].match]:
                owner[res.role.id] = index
    return [
        res if res.role is None or owner[res.role.id] == index
        else RoleResolution(res.spec, None, note=f"« {res.role.name} » sert déjà pour un autre rôle", ambiguous=True)
        for index, res in enumerate(resolutions)
    ]


# --- Construction du plan --------------------------------------------------------------------


@dataclass
class _Builder:
    layout: ServerLayout
    snapshot: GuildSnapshot
    target: Target
    options: RedesignOptions
    actor: Actor
    admin_role_name: str | None
    roles: list[RoleResolution] = field(default_factory=list)
    ops: list[Operation] = field(default_factory=list)
    deletions: list[Operation] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def bot_can(self, permission: str) -> bool:
        perms = self.snapshot.bot_permissions
        return "administrator" in perms or permission in perms

    def add(self, op: Operation) -> None:
        self.ops.append(op)


def build_plan(
    layout: ServerLayout,
    snapshot: GuildSnapshot,
    *,
    saved_bindings: dict[str, int] | None = None,
    saved_categories: dict[str, int] | None = None,
    saved_roles: dict[str, int] | None = None,
    options: RedesignOptions = RedesignOptions(),
    alerts_channel: str | None = None,
    admin_role_name: str | None = None,
    actor: Actor = OWNER,
) -> RedesignPlan:
    """Plan de refonte : uniquement de la lecture, aucun effet de bord."""
    target = build_target(layout, options.profile, options.style)
    saved_bindings = dict(saved_bindings or {})
    report = resolve_structure(layout, snapshot, saved_bindings, saved_categories)
    roles = resolve_roles(layout, snapshot, saved_roles)
    b = _Builder(layout, snapshot, target, options, actor, admin_role_name, roles)

    available_roles = _plan_roles(b, roles)
    category_ok = _plan_categories(b, report, available_roles)
    created_channels = _plan_channels(b, report, available_roles, category_ok)
    _plan_positions(b, report, roles, created_channels)
    _plan_bindings(b, report, saved_bindings, created_channels, alerts_channel)
    _plan_outside(b, report, roles)
    _plan_risks(b)

    blockers = _blockers(b, report)
    if options.deletions and not layout.policy.deletions_allowed:
        options = RedesignOptions(**(asdict(options) | {"deletions": False}))
        b.risks.append("Les suppressions sont désactivées par [refonte] deletions_allowed = false : la liste reste informative.")
    return RedesignPlan(
        tuple(b.ops), options, tuple(blockers), tuple(dict.fromkeys(b.risks)), tuple(b.deletions), target, report, tuple(roles)
    )


def _wanted(spec: ChannelSpec | RoleSpec, exists: bool, b: _Builder) -> bool:
    return b.target.in_profile(spec) and (exists or not spec.optional or b.options.create_optional)


# --- Rôles ------------------------------------------------------------------------------------


def _plan_roles(b: _Builder, roles: list[RoleResolution]) -> set[str]:
    """Rôles créés, complétés ou réutilisés. Retourne les clés de rôles qui existeront après la refonte."""
    available: set[str] = set()
    for res in roles:
        spec = res.spec
        if not _wanted(spec, res.role is not None, b):
            if res.role is not None:
                available.add(spec.key)
                b.add(Operation(OpKind.KEEP, spec.key, "role", spec.name, res.role.id, res.role.name,
                                expected="rôle reconnu (hors profil) : conservé"))
            elif b.target.in_profile(spec):
                b.add(Operation(OpKind.SKIP, spec.key, "role", spec.name,
                                params=_role_params(spec), expected="rôle facultatif : créé seulement avec facultatifs:True"))
            continue
        if res.ambiguous:
            b.add(Operation(OpKind.MANUAL, spec.key, "role", spec.name,
                            expected=f"{res.note} : renommez ou supprimez le doublon, puis relancez l'analyse"))
            continue
        role = res.role
        if role is None:
            if not b.options.manage_roles:
                b.add(Operation(OpKind.SKIP, spec.key, "role", spec.name, expected="rôles non gérés (option roles:False)"))
            elif not b.bot_can("manage_roles"):
                b.add(Operation(OpKind.MANUAL, spec.key, "role", spec.name, expected="Samae n'a pas « Gérer les rôles »"))
            elif not b.actor.can("manage_roles"):
                b.add(Operation(OpKind.MANUAL, spec.key, "role", spec.name, expected="vous n'avez pas « Gérer les rôles »"))
            else:
                granted, refused = _grantable(b, spec.permissions)
                params = _role_params(spec) | {"permissions": granted}
                expected = f"nouveau rôle {_color(spec.color)}" + (f", permissions : {_perm_list(granted)}" if granted else ", sans permission")
                b.add(Operation(OpKind.CREATE_ROLE, spec.key, "role", spec.name, params=params, expected=expected))
                available.add(spec.key)
                if refused:
                    b.add(Operation(OpKind.MANUAL, f"{spec.key}:permissions", "role", spec.name,
                                    expected=f"permissions non accordées (Samae ou vous ne les avez pas) : {_perm_list(refused)}"))
            continue
        available.add(spec.key)
        params, before, changes = _role_changes(b, spec, role)
        if not changes:
            b.add(Operation(OpKind.KEEP, spec.key, "role", spec.name, role.id, role.name,
                            expected=f"rôle réutilisé, déjà conforme {_color(role.color)}"))
            continue
        blocked = _role_blocked(b, role)
        if blocked or not b.options.manage_roles or not b.layout.policy.edit_roles:
            reason = blocked or ("rôles non gérés (option roles:False)" if not b.options.manage_roles else "[refonte] edit_roles = false")
            kind = OpKind.MANUAL if blocked else OpKind.SKIP
            b.add(Operation(kind, spec.key, "role", spec.name, role.id, role.name, changes=tuple(changes),
                            expected=f"rôle réutilisé, non modifié : {reason}"))
            continue
        refused = params.pop("refused", [])
        b.add(Operation(OpKind.EDIT_ROLE, spec.key, "role", spec.name, role.id, role.name,
                        params=params | {"before": before}, changes=tuple(changes), expected="rôle existant réutilisé et complété"))
        if refused:
            b.add(Operation(OpKind.MANUAL, f"{spec.key}:permissions", "role", spec.name, role.id,
                            expected=f"permissions non accordées (Samae ou vous ne les avez pas) : {_perm_list(refused)}"))
        extra = sorted((role.permissions & SENSITIVE_PERMISSIONS) - set(spec.permissions))
        if "administrator" in role.permissions:
            extra.insert(0, "administrator")
        if extra:
            b.risks.append(f"Le rôle « {role.name} » a des permissions sensibles hors configuration "
                           f"({_perm_list(extra)}) : Samae ne retire jamais de permission, vérifiez-les.")
    return available


def _role_params(spec: RoleSpec) -> dict:
    return {"name": spec.name, "color": spec.color, "hoist": spec.hoist, "mentionable": spec.mentionable,
            "permissions": list(spec.permissions)}


def _grantable(b: _Builder, permissions: tuple[str, ...]) -> tuple[list[str], list[str]]:
    granted = [p for p in permissions if b.bot_can(p) and b.actor.can(p)]
    return granted, [p for p in permissions if p not in granted]


def _role_blocked(b: _Builder, role: GuildRole) -> str:
    if role.position >= b.snapshot.bot_top_role_position:
        return "rôle placé au-dessus de celui de Samae (hiérarchie Discord)"
    if not b.actor.outranks(role.position):
        return "rôle placé au-dessus du vôtre"
    if not b.bot_can("manage_roles"):
        return "Samae n'a pas « Gérer les rôles »"
    if not b.actor.can("manage_roles"):
        return "vous n'avez pas « Gérer les rôles »"
    return ""


def _role_changes(b: _Builder, spec: RoleSpec, role: GuildRole) -> tuple[dict, dict, list[str]]:
    params: dict = {}
    changes: list[str] = []
    if role.name != spec.name:
        params["name"] = spec.name
        changes.append(f"nom : « {role.name} » → « {spec.name} »")
    if spec.color is not None and role.color != spec.color:
        params["color"] = spec.color
        changes.append(f"couleur : {_hex(role.color)} → {_hex(spec.color)}")
    if role.hoist != spec.hoist:
        params["hoist"] = spec.hoist
        changes.append("affiché séparément" if spec.hoist else "n'est plus affiché séparément")
    if role.mentionable != spec.mentionable:
        params["mentionable"] = spec.mentionable
        changes.append("mentionnable" if spec.mentionable else "non mentionnable")
    missing = [p for p in spec.permissions if p not in role.permissions]
    if missing:
        granted, refused = _grantable(b, tuple(missing))
        if granted:
            params["add_permissions"] = granted
            changes.append(f"permissions ajoutées : {_perm_list(granted)}")
        if refused:
            params["refused"] = refused
    before = {"name": role.name, "color": role.color, "hoist": role.hoist, "mentionable": role.mentionable,
              "permissions": sorted(role.permissions)}
    return params, before, changes


# --- Catégories -------------------------------------------------------------------------------


def _plan_categories(b: _Builder, report: StructureReport, available_roles: set[str]) -> dict[str, bool]:
    """Retourne, pour chaque catégorie de la cible, si elle existera après la refonte."""
    category_ok: dict[str, bool] = {}
    channel_res = {res.spec.key: res for res in report.channels}
    for res in report.categories:
        spec = res.spec
        wanted_channels = [c for c in b.target.channels_of(spec.key) if _wanted(c, channel_res[c.key].channel is not None, b)]
        name = b.target.name(spec)
        cat = res.category
        if cat is not None:
            category_ok[spec.key] = True
            if cat.name != name:
                if cat.manageable:
                    b.add(Operation(OpKind.EDIT_CATEGORY, spec.key, "category", name, cat.id, cat.name,
                                    params={"name": name, "before": {"name": cat.name}},
                                    changes=(f"« {cat.name} » → « {name} »",), expected="catégorie renommée"))
                else:
                    b.add(Operation(OpKind.MANUAL, spec.key, "category", name, cat.id, cat.name,
                                    expected="Samae ne peut pas modifier cette catégorie (« Gérer les salons » refusé)"))
            else:
                b.add(Operation(OpKind.KEEP, spec.key, "category", name, cat.id, cat.name, expected="déjà conforme"))
            _plan_permissions(b, spec, "category", name, cat.id, cat.overwrites, available_roles, manageable=cat.manageable)
        elif res.ambiguous:
            category_ok[spec.key] = False
            b.add(Operation(OpKind.MANUAL, spec.key, "category", name,
                            expected=f"{res.note} : renommez ou regroupez les doublons, puis relancez l'analyse"))
        elif wanted_channels:
            category_ok[spec.key] = True
            rules = _concrete_rules(b, spec, available_roles)
            b.add(Operation(OpKind.CREATE_CATEGORY, spec.key, "category", name,
                            params={"name": name, "overwrites": _serialize_rules(rules)},
                            expected="nouvelle catégorie" + (f", accès « {b.target.access_of(spec).label} »" if b.target.access_of(spec) else "")))
        else:
            category_ok[spec.key] = False
            b.add(Operation(OpKind.SKIP, spec.key, "category", name, expected="aucun salon à y ranger dans ce profil"))
    return category_ok


# --- Salons -----------------------------------------------------------------------------------


def _plan_channels(
    b: _Builder, report: StructureReport, available_roles: set[str], category_ok: dict[str, bool]
) -> dict[str, str]:
    """Retourne les salons créés : clé → catégorie."""
    category_res = {res.spec.key: res for res in report.categories}
    categories_by_id = {cat.id: cat for cat in b.snapshot.categories}
    taken = Counter((normalize_name(ch.name), TYPE_FAMILY.get(ch.type, ch.type)) for ch in b.snapshot.channels)
    created: dict[str, str] = {}
    incoming: Counter[str] = Counter()
    for res in report.channels:
        spec = res.spec
        name = b.target.name(spec)
        cat_res = category_res[spec.category_key]
        cat_id = cat_res.category.id if cat_res.category else None
        cat_name = b.target.name(b.layout.categories[spec.category_key])
        exists = res.usable and res.channel is not None
        if not _wanted(spec, res.channel is not None, b):
            if res.channel is not None:
                b.add(Operation(OpKind.KEEP, spec.key, spec.type, name, res.channel.id, res.channel.name,
                                expected="salon reconnu (hors profil) : conservé tel quel"))
            elif b.target.in_profile(spec):
                b.add(Operation(OpKind.SKIP, spec.key, spec.type, name,
                                expected="salon facultatif : créé seulement avec facultatifs:True"))
            continue
        if exists:
            ch = res.channel
            channel_ops: list[Operation] = []
            if not ch.manageable:
                b.add(Operation(OpKind.MANUAL, spec.key, spec.type, name, ch.id, ch.name,
                                expected="Samae ne peut pas modifier ce salon (« Gérer les salons » refusé dans le salon)"))
                continue
            edit_params, before, changes, notes = _channel_changes(b, spec, ch, name, taken)
            for note in notes:
                b.add(Operation(OpKind.MANUAL, f"{spec.key}:note", spec.type, name, ch.id, ch.name, expected=note))
            if edit_params:
                channel_ops.append(Operation(OpKind.EDIT_CHANNEL, spec.key, spec.type, name, ch.id, ch.name,
                                             params=edit_params | {"before": before}, changes=tuple(changes),
                                             expected="historique et identifiant conservés"))
            if ch.category_id != cat_id or cat_id is None:
                current_cat = categories_by_id.get(ch.category_id or 0)
                current_label = current_cat.name if current_cat else "hors catégorie"
                if not category_ok.get(spec.category_key):
                    channel_ops.append(Operation(OpKind.MANUAL, spec.key, spec.type, name, ch.id, ch.name,
                                                 expected=f"catégorie « {cat_name} » indisponible : salon laissé dans « {current_label} »"))
                else:
                    incoming[spec.category_key] += 1
                    channel_ops.append(Operation(OpKind.MOVE_CHANNEL, spec.key, spec.type, name, ch.id, current_label,
                                                 category_key=spec.category_key,
                                                 params={"category": f"cat:{spec.category_key}", "before": {"category_id": ch.category_id}},
                                                 changes=(f"de « {current_label} » vers « {cat_name} »",),
                                                 expected="permissions corrigées ensuite si besoin"))
            b.ops.extend(channel_ops)
            permission_op = _plan_permissions(b, spec, spec.type, name, ch.id, ch.overwrites, available_roles, manageable=True)
            if not channel_ops and not permission_op:
                b.add(Operation(OpKind.KEEP, spec.key, spec.type, name, ch.id, ch.name, expected="déjà conforme"))
        elif res.status is ChannelStatus.MISSING:
            if not category_ok.get(spec.category_key):
                b.add(Operation(OpKind.MANUAL, spec.key, spec.type, name,
                                expected=f"catégorie « {cat_name} » indisponible : salon non créé"))
            elif taken[(normalize_name(spec.name), TYPE_FAMILY[spec.type])]:
                b.add(Operation(OpKind.MANUAL, spec.key, spec.type, name,
                                expected="un salon de ce nom existe déjà mais ne peut pas être associé : vérifiez les doublons"))
            else:
                created_type = spec.type if b.snapshot.community or spec.type not in COMMUNITY_FALLBACK else COMMUNITY_FALLBACK[spec.type]
                rules = _concrete_rules(b, spec, available_roles)
                incoming[spec.category_key] += 1
                note = ""
                if created_type != spec.type:
                    note = f" (salon {TYPE_LABELS[created_type]} : le type {TYPE_LABELS[spec.type]} demande la fonction Communauté)"
                b.add(Operation(OpKind.CREATE_CHANNEL, spec.key, created_type, name, category_key=spec.category_key,
                                params={"name": name, "type": created_type, "topic": spec.topic, "category": f"cat:{spec.category_key}",
                                        "overwrites": _serialize_rules(rules)},
                                expected=f"nouveau salon {TYPE_LABELS[created_type]} dans « {cat_name} »" + _access_note(b, spec) + note))
                created[spec.key] = spec.category_key
                taken[(normalize_name(spec.name), TYPE_FAMILY[spec.type])] += 1
        else:  # AMBIGUOUS ou WRONG_TYPE : jamais de choix au hasard, jamais de doublon créé
            b.add(Operation(OpKind.MANUAL, spec.key, spec.type, name,
                            res.channel.id if res.channel else None, res.channel.name if res.channel else "",
                            expected=f"{res.note} : à régler à la main, puis relancez l'analyse"))
    for key, count in incoming.items():
        res = category_res[key]
        existing = sum(1 for ch in b.snapshot.channels if res.category and ch.category_id == res.category.id)
        if existing + count > MAX_CATEGORY_CHANNELS:
            b.risks.append(f"La catégorie « {b.target.name(b.layout.categories[key])} » dépasserait {MAX_CATEGORY_CHANNELS} "
                           "salons : Discord refusera une partie des opérations.")
    return created


def _access_note(b: _Builder, spec: ChannelSpec) -> str:
    access = b.target.access_of(spec)
    if access is None or not b.options.manage_permissions or not b.layout.policy.permissions:
        return ""
    return f", accès « {access.label} »"


def _channel_changes(b: _Builder, spec: ChannelSpec, ch: GuildChannel, name: str, taken: Counter):
    params: dict = {}
    before = {"name": ch.name, "topic": ch.topic, "type": ch.type}
    changes: list[str] = []
    notes: list[str] = []
    if ch.name != name:
        same = normalize_name(name) == normalize_name(ch.name)
        if not same and taken[(normalize_name(spec.name), TYPE_FAMILY.get(ch.type, ch.type))] > 0:
            notes.append(f"un autre salon s'appelle déjà « {spec.name} » : renommage annulé pour éviter un doublon")
        else:
            params["name"] = name
            changes.append(f"nom : « {ch.name} » → « {name} »")
    if spec.topic and TYPE_FAMILY.get(ch.type) in ("text", "forum") and ch.topic != spec.topic:
        if not ch.topic:
            params["topic"] = spec.topic
            changes.append("sujet ajouté")
        else:
            notes.append("sujet personnalisé conservé (le sujet proposé n'est appliqué qu'à un salon sans sujet)")
    if spec.type != ch.type:
        convertible = {spec.type, ch.type} == {"text", "announcement"}
        if convertible and b.snapshot.community:
            params["type"] = spec.type
            changes.append(f"type : salon {TYPE_LABELS[ch.type]} → salon {TYPE_LABELS[spec.type]}")
        elif convertible:
            notes.append(f"reste un salon {TYPE_LABELS[ch.type]} : le type {TYPE_LABELS[spec.type]} demande la fonction Communauté")
        else:
            notes.append(f"salon {TYPE_LABELS.get(ch.type, ch.type)} conservé (Discord ne convertit pas vers {TYPE_LABELS[spec.type]})")
    return params, before, changes, notes


# --- Permissions --------------------------------------------------------------------------------


def _admin_role(b: _Builder) -> GuildRole | None:
    if not b.admin_role_name:
        return None
    wanted = b.admin_role_name.casefold()
    return next((r for r in b.snapshot.roles if r.name.casefold() == wanted and not r.is_default), None)


def _concrete_rules(b: _Builder, spec: CategorySpec | ChannelSpec, available_roles: set[str]) -> dict[str, Rule]:
    if not b.target.manages_permissions or not b.options.manage_permissions or not b.layout.policy.permissions:
        return {}
    return b.target.rules(spec, available_roles, admin_role=_admin_role(b) is not None)


def _target_id(b: _Builder, target: str, roles: dict[str, int]) -> tuple[str, int | None]:
    if target == "@everyone":
        return "role", b.snapshot.guild_id
    if target == "@bot":
        return "member", b.snapshot.bot_id
    if target == "@admin":
        role = _admin_role(b)
        return "role", role.id if role else None
    return "role", roles.get(target[5:])


def _plan_permissions(
    b: _Builder,
    spec: CategorySpec | ChannelSpec,
    subject: str,
    name: str,
    object_id: int,
    overwrites: tuple[Overwrite, ...],
    available_roles: set[str],
    *,
    manageable: bool,
) -> bool:
    """Ajoute une correction de permissions si les cibles gérées diffèrent de la cible. Retourne True si une opération est ajoutée."""
    rules = _concrete_rules(b, spec, available_roles)
    if not rules:
        return False
    role_ids = {res.spec.key: res.role.id for res in b.roles if res.role is not None}
    current = {(o.target_type, o.target_id): (o.allow, o.deny) for o in overwrites}
    changed: dict[str, Rule] = {}
    before: dict[str, Rule] = {}
    ids: dict[str, list] = {}
    for target, wanted in rules.items():
        kind, target_id = _target_id(b, target, role_ids)
        now = current.get((kind, target_id), NEUTRAL) if target_id is not None else NEUTRAL
        if now != wanted:
            changed[target], before[target], ids[target] = wanted, now, [kind, target_id]
    if not changed:
        return False
    key = spec.key
    lines = tuple(_describe_rule(b, t, before[t], changed[t]) for t in changed)
    used = set().union(*(allow | deny for allow, deny in changed.values()))
    missing = sorted(p for p in used if not b.bot_can(p))
    if not manageable or not b.bot_can("manage_roles") or missing or not b.actor.can("manage_roles"):
        if not manageable:
            reason = "Samae ne peut pas modifier cet élément"
        elif not b.bot_can("manage_roles"):
            reason = "Samae n'a pas « Gérer les rôles », nécessaire pour modifier des permissions"
        elif missing:
            reason = f"Samae ne peut pas accorder ou refuser ce qu'il n'a pas : {_perm_list(missing)}"
        else:
            reason = "vous n'avez pas « Gérer les rôles », nécessaire pour modifier des permissions"
        b.add(Operation(OpKind.MANUAL, f"{key}:permissions", subject, name, object_id, changes=lines,
                        expected=f"permissions non corrigées : {reason}"))
        return True
    b.add(Operation(OpKind.SET_PERMISSIONS, key, subject, name, object_id,
                    params={"targets": _serialize_rules(changed), "before": _serialize_rules(before), "ids": ids},
                    changes=lines, expected="seules les cibles gérées changent ; les autres permissions sont conservées"))
    return True


def _serialize_rules(rules: dict[str, Rule]) -> dict[str, dict[str, list[str]]]:
    return {target: {"allow": sorted(allow), "deny": sorted(deny)} for target, (allow, deny) in rules.items()}


def _target_label(b: _Builder, target: str) -> str:
    if target == "@bot":
        return "Samae"
    if target == "@admin":
        return f"rôle {b.admin_role_name}"
    if target.startswith("role:"):
        spec = b.layout.roles.get(target[5:])
        return f"rôle {spec.name}" if spec else target
    return target


def _describe_rule(b: _Builder, target: str, before: Rule, after: Rule) -> str:
    label = _target_label(b, target)
    allow, deny = after
    if not allow and not deny:
        return f"{label} : permissions particulières retirées (héritées du serveur)"
    parts = []
    if allow:
        parts.append(f"autorise {_perm_list(sorted(allow))}")
    if deny:
        parts.append(f"refuse {_perm_list(sorted(deny))}")
    return f"{label} : " + " ; ".join(parts)


# --- Positions ----------------------------------------------------------------------------------


def _family_rank(channel_type: str) -> int:
    return 1 if TYPE_FAMILY.get(channel_type) == "voice" else 0


def _plan_positions(b: _Builder, report: StructureReport, roles: list[RoleResolution], created: dict[str, str]) -> None:
    if not b.layout.policy.positions:
        return
    _plan_role_order(b, roles)
    target_categories = [res for res in report.categories if any(
        op.key == res.spec.key and op.kind in (OpKind.KEEP, OpKind.EDIT_CATEGORY, OpKind.CREATE_CATEGORY) and op.subject == "category"
        for op in b.ops)]
    target_cat_ids = {res.category.id for res in target_categories if res.category}
    current_categories = sorted(b.snapshot.categories, key=lambda c: (c.position, c.id))
    desired = [f"cat:{res.spec.key}" for res in target_categories] + [
        f"id:{c.id}" for c in current_categories if c.id not in target_cat_ids
    ]
    ref_of = {res.category.id: f"cat:{res.spec.key}" for res in target_categories if res.category}
    simulated = [ref_of.get(c.id, f"id:{c.id}") for c in current_categories] + [
        f"cat:{res.spec.key}" for res in target_categories if res.category is None
    ]
    if simulated != desired:
        names = [b.target.name(res.spec) for res in target_categories]
        if b.bot_can("manage_channels"):
            b.add(Operation(OpKind.REORDER_CATEGORIES, "categories", "category", "catégories",
                            params={"order": desired}, changes=(" → ".join(names),),
                            expected="catégories de la cible en tête, dans l'ordre du fichier ; les autres ensuite"))

    # Salons : dans chaque catégorie de la cible, ses salons dans l'ordre du fichier, puis les autres.
    channel_res = {res.spec.key: res for res in report.channels}
    moved_in = {op.key: op.category_key for op in b.ops if op.kind is OpKind.MOVE_CHANNEL}
    moved_out_ids = {op.object_id for op in b.ops if op.kind is OpKind.MOVE_CHANNEL}
    placed = {op.key for op in b.ops if op.kind in (OpKind.KEEP, OpKind.EDIT_CHANNEL, OpKind.MOVE_CHANNEL, OpKind.SET_PERMISSIONS)
              and op.subject not in ("category", "role", "config")}
    matched_ids = {res.channel.id: res.spec.key for res in report.channels if res.channel is not None and res.usable}
    for res in target_categories:
        key = res.spec.key
        cat_id = res.category.id if res.category else None
        members = [ch for ch in b.snapshot.channels if cat_id is not None and ch.category_id == cat_id and ch.id not in moved_out_ids]
        members.sort(key=lambda ch: (_family_rank(ch.type), ch.position, ch.id))
        targets = []
        for spec in b.target.channels_of(key):
            r = channel_res[spec.key]
            in_category = (r.channel is not None and r.channel.category_id == cat_id and r.usable) or moved_in.get(spec.key) == key
            if spec.key in created or (spec.key in placed and in_category):
                kind = created_type(b, spec) if spec.key in created else (r.channel.type if r.channel else spec.type)
                targets.append((_family_rank(kind), f"chan:{spec.key}"))
        if not targets:
            continue
        target_refs = {ref for _, ref in targets}
        others = [(_family_rank(ch.type), f"id:{ch.id}") for ch in members
                  if f"chan:{matched_ids.get(ch.id)}" not in target_refs]
        # Les salons cibles d'abord, puis les autres, famille par famille (textes au-dessus des vocaux).
        ordered: list[str] = []
        simulated: list[str] = []  # ordre obtenu sans réordonner : Discord ajoute les nouveaux salons en bas
        for rank in (0, 1):
            ordered += [ref for r_, ref in targets if r_ == rank] + [ref for r_, ref in others if r_ == rank]
            simulated += [
                f"chan:{matched_ids[ch.id]}" if f"chan:{matched_ids.get(ch.id)}" in target_refs else f"id:{ch.id}"
                for ch in members if _family_rank(ch.type) == rank
            ] + [ref for r_, ref in targets if r_ == rank and ref[5:] in created]
        if simulated != ordered or any(ref[5:] in moved_in for ref in target_refs):
            b.add(Operation(OpKind.REORDER_CHANNELS, f"{key}:order", "category", b.target.name(res.spec), cat_id,
                            category_key=key, params={"category": f"cat:{key}", "order": ordered},
                            changes=(f"{len(targets)} salon(s) de la cible dans l'ordre du fichier, {len(others)} autre(s) ensuite",),
                            expected="ordre des salons dans la catégorie"))


def created_type(b: _Builder, spec: ChannelSpec) -> str:
    return spec.type if b.snapshot.community or spec.type not in COMMUNITY_FALLBACK else COMMUNITY_FALLBACK[spec.type]


def _plan_role_order(b: _Builder, roles: list[RoleResolution]) -> None:
    if not b.options.manage_roles or not b.bot_can("manage_roles") or not b.actor.can("manage_roles"):
        return
    created = {op.key for op in b.ops if op.kind is OpKind.CREATE_ROLE}
    order: list[str] = []
    existing: list[GuildRole] = []
    for res in roles:
        if res.spec.key in created:
            order.append(f"role:{res.spec.key}")
        elif res.role is not None and b.target.in_profile(res.spec) and not _role_blocked(b, res.role):
            order.append(f"role:{res.spec.key}")
            existing.append(res.role)
    if len(order) < 2:
        return
    current = [r.id for r in sorted(existing, key=lambda r: -r.position)]
    if current != [r.id for r in existing] or created:
        names = [b.layout.roles[ref[5:]].name for ref in order]
        b.add(Operation(OpKind.REORDER_ROLES, "roles", "role", "hiérarchie", params={"order": order},
                        changes=(" > ".join(names),), expected="rôles de la cible ordonnés comme dans le fichier, sous le rôle de Samae"))


# --- Configuration de Samae ---------------------------------------------------------------------


def _plan_bindings(
    b: _Builder, report: StructureReport, saved: dict[str, int], created: dict[str, str], alerts_channel: str | None
) -> None:
    for res in report.channels:
        spec = res.spec
        current = saved.get(spec.key)
        if spec.key in created:
            b.add(Operation(OpKind.BIND, spec.key, "config", b.target.name(spec),
                            expected=f"fonction « {spec.key} » associée au nouveau salon"))
        elif res.usable and res.channel is not None and current != res.channel.id:
            b.add(Operation(OpKind.BIND, spec.key, "config", b.target.name(spec), res.channel.id, str(current or ""),
                            expected=f"fonction « {spec.key} » associée au salon existant « {res.channel.name} » (par identifiant)"))
        elif current is not None and not res.usable:
            b.add(Operation(OpKind.BIND, spec.key, "config", b.target.name(spec), None, str(current),
                            expected=f"association « {spec.key} » retirée : le salon enregistré est introuvable"))
    if "logs" in b.layout.channels and alerts_channel != "logs":
        logs = next(res for res in report.channels if res.spec.key == "logs")
        if logs.usable or "logs" in created:
            current = f"ALERTS_CHANNEL={alerts_channel}" if alerts_channel else "ALERTS_CHANNEL vide"
            b.add(Operation(OpKind.MANUAL, "alerts_channel", "config", "logs",
                            expected=f"{current} : pour recevoir les alertes dans le journal du bot, mettez ALERTS_CHANNEL=logs "
                                     "dans .env puis redémarrez"))


# --- Éléments hors structure : personnalisés, inconnus, doublons, suppressions -------------------


def _plan_outside(b: _Builder, report: StructureReport, roles: list[RoleResolution]) -> None:
    keep = {normalize_name(name) for name in b.layout.policy.keep}
    obsolete = {normalize_name(name) for name in b.layout.policy.obsolete}
    matched_channels = {res.channel.id: res for res in report.channels if res.channel is not None}
    matched_categories = {res.category.id for res in report.categories if res.category is not None}
    # Nom, nom stylé et alias de chaque salon cible : un homonyme est un doublon, pas un inconnu.
    target_names = {}
    for res in report.channels:
        for alias in (res.spec.name, b.target.name(res.spec), *res.spec.aliases):
            target_names.setdefault((normalize_name(alias), TYPE_FAMILY[res.spec.type]), res)
    moved_out = Counter(
        next((ch.category_id for ch in b.snapshot.channels if ch.id == op.object_id), None)
        for op in b.ops if op.kind is OpKind.MOVE_CHANNEL
    )
    protected = {res.channel.id for res in report.channels if res.channel is not None}
    removable = b.bot_can("manage_channels") and b.actor.can("manage_channels")

    for ch in sorted(b.snapshot.channels, key=lambda c: (c.category_id or 0, c.position, c.id)):
        if ch.id in matched_channels:
            continue
        norm = normalize_name(ch.name)
        family = TYPE_FAMILY.get(ch.type, ch.type)
        twin = target_names.get((norm, family))
        if norm in keep:
            b.add(Operation(OpKind.CUSTOM, f"id:{ch.id}", ch.type, ch.name, ch.id, ch.name, expected="personnalisé : conservé tel quel"))
        elif twin is not None:
            twin_name = twin.channel.name if twin.channel else b.target.name(twin.spec)
            note = (f"même nom que « {twin_name} » (fonction « {twin.spec.key} ») : conservé, à vérifier" if twin.channel else
                    f"plusieurs salons peuvent servir de « {twin_name} » (fonction « {twin.spec.key} ») : aucun n'est choisi au hasard, "
                    "renommez ou supprimez le doublon")
            b.add(Operation(OpKind.DUPLICATE, f"id:{ch.id}", ch.type, ch.name, ch.id, ch.name, expected=note))
            if family == "text" and not ch.has_messages and ch.id not in protected and removable:
                b.deletions.append(Operation(OpKind.DELETE_CHANNEL, f"id:{ch.id}", ch.type, ch.name, ch.id, ch.name,
                                             params={"id": ch.id, "name": ch.name, "require_empty": True},
                                             expected=f"doublon vide de « {twin_name} » : aucun message"))
        else:
            b.add(Operation(OpKind.UNKNOWN, f"id:{ch.id}", ch.type, ch.name, ch.id, ch.name,
                            expected="hors de la structure cible : conservé" + (", rangé après les salons de la cible" if ch.category_id in matched_categories else "")))
        if norm in obsolete and ch.id not in protected and removable and not any(d.object_id == ch.id for d in b.deletions):
            warning = "contient des messages : ils seront perdus définitivement" if ch.has_messages else "aucun message"
            b.deletions.append(Operation(OpKind.DELETE_CHANNEL, f"id:{ch.id}", ch.type, ch.name, ch.id, ch.name,
                                         params={"id": ch.id, "name": ch.name, "require_empty": False},
                                         expected=f"déclaré obsolète dans [refonte] obsolete ; {warning}"))

    for cat in sorted(b.snapshot.categories, key=lambda c: (c.position, c.id)):
        if cat.id in matched_categories:
            continue
        norm = normalize_name(cat.name)
        content = sum(1 for ch in b.snapshot.channels if ch.category_id == cat.id)
        left = content - moved_out.get(cat.id, 0)
        kind = OpKind.CUSTOM if norm in keep else OpKind.UNKNOWN
        note = "personnalisée : conservée telle quelle" if kind is OpKind.CUSTOM else "hors de la structure cible : conservée"
        if left == 0:
            note += " ; vide après la refonte" if content else " ; déjà vide"
        b.add(Operation(kind, f"id:{cat.id}", "category", cat.name, cat.id, cat.name, expected=note))
        if left == 0 and kind is not OpKind.CUSTOM and removable:
            b.deletions.append(Operation(OpKind.DELETE_CATEGORY, f"id:{cat.id}", "category", cat.name, cat.id, cat.name,
                                         params={"id": cat.id, "name": cat.name},
                                         expected="catégorie hors structure, vide" + (" après les déplacements" if content else "")))
        elif norm in obsolete and left:
            b.risks.append(f"La catégorie obsolète « {cat.name} » contient encore {left} salon(s) : elle n'est pas proposée à la suppression.")

    matched_roles = {res.role.id for res in roles if res.role is not None}
    role_names = {normalize_name(res.spec.name): res for res in roles}
    for role in sorted(b.snapshot.roles, key=lambda r: -r.position):
        if role.is_default or role.managed or role.id in matched_roles:
            continue
        norm = normalize_name(role.name)
        twin = role_names.get(norm)
        if norm in keep:
            b.add(Operation(OpKind.CUSTOM, f"id:{role.id}", "role", role.name, role.id, role.name, expected="personnalisé : conservé tel quel"))
        elif twin is not None:
            b.add(Operation(OpKind.DUPLICATE, f"id:{role.id}", "role", role.name, role.id, role.name,
                            expected=f"même nom que le rôle « {twin.spec.name} » : conservé, à vérifier"))
        else:
            b.add(Operation(OpKind.UNKNOWN, f"id:{role.id}", "role", role.name, role.id, role.name,
                            expected="hors de la structure cible : conservé, jamais modifié"))
        deletable = (role.position < b.snapshot.bot_top_role_position and b.actor.outranks(role.position)
                     and b.bot_can("manage_roles") and b.actor.can("manage_roles") and not role.dangerous)
        if norm in obsolete and deletable:
            b.deletions.append(Operation(OpKind.DELETE_ROLE, f"id:{role.id}", "role", role.name, role.id, role.name,
                                         params={"id": role.id, "name": role.name},
                                         expected="déclaré obsolète dans [refonte] obsolete ; retiré à tous les membres qui l'ont"))


# --- Risques et points bloquants -----------------------------------------------------------------


def _plan_risks(b: _Builder) -> None:
    for name, count in _duplicates(b.snapshot.channels):
        b.risks.append(f"{count} salons s'appellent « {name} » : Samae ne choisit jamais au hasard entre eux.")
    admins = [r.name for r in b.snapshot.roles if "administrator" in r.dangerous and not r.managed and not r.is_default]
    if len(admins) > 2:
        b.risks.append(f"{len(admins)} rôles ont la permission Administrateur ({', '.join(admins[:5])}) : vérifiez que c'est voulu (non modifié).")
    if any(op.kind is OpKind.EDIT_CHANNEL and "name" in op.params for op in b.ops):
        b.risks.append("Des messages écrits à la main peuvent encore citer les anciens noms de salons ; les fonctions de Samae "
                       "(messages permanents, réunions, notifications) utilisent les identifiants et continuent de fonctionner.")
    hidden = [op.target for op in b.ops if op.kind in (OpKind.SET_PERMISSIONS, OpKind.CREATE_CATEGORY) and op.subject == "category"
              and "view_channel" in (op.params.get("targets") or op.params.get("overwrites") or {}).get("@everyone", {}).get("deny", [])]
    for name in hidden:
        b.risks.append(f"« {name} » sera réservée au staff : @everyone ne la verra plus (propriétaire et administrateurs gardent l'accès).")
    if not b.snapshot.community and any(spec.type in COMMUNITY_FALLBACK for spec in b.layout.channels.values() if b.target.in_profile(spec)):
        b.risks.append("Le serveur n'a pas la fonction Communauté : annonces, forums, médias et conférences sont remplacés par "
                       "des salons textuels ou vocaux (Paramètres du serveur > Activer la Communauté, puis relancez).")
    above = [res.spec.name for res in b.roles if res.role is not None and res.role.position >= b.snapshot.bot_top_role_position]
    if above:
        b.risks.append(f"Rôles au-dessus de celui de Samae (non modifiables) : {', '.join(above)}. Montez le rôle de Samae "
                       "dans Paramètres du serveur > Rôles si vous voulez qu'il les gère.")


def _duplicates(channels: tuple[GuildChannel, ...]) -> list[tuple[str, int]]:
    counter = Counter((normalize_name(ch.name), TYPE_FAMILY.get(ch.type, ch.type)) for ch in channels)
    names = {}
    for ch in channels:
        names.setdefault((normalize_name(ch.name), TYPE_FAMILY.get(ch.type, ch.type)), ch.name)
    return sorted((names[key], count) for key, count in counter.items() if count > 1 and key[0])


def _blockers(b: _Builder, report: StructureReport) -> list[str]:
    blockers = []
    channel_ops = [op for op in b.ops if op.actionable and op.kind not in (OpKind.BIND, OpKind.CREATE_ROLE, OpKind.EDIT_ROLE, OpKind.REORDER_ROLES)]
    if channel_ops and not b.bot_can("manage_channels"):
        blockers.append("Samae n'a pas la permission « Gérer les salons » : Paramètres du serveur > Rôles > rôle de Samae > "
                        "cochez « Gérer les salons ».")
    created = sum(1 for op in b.ops if op.kind in (OpKind.CREATE_CATEGORY, OpKind.CREATE_CHANNEL))
    total = len(b.snapshot.categories) + len(b.snapshot.channels)
    if created and total + created > MAX_GUILD_CHANNELS:
        blockers.append(f"Le serveur dépasserait la limite Discord de {MAX_GUILD_CHANNELS} salons ({total} existants, {created} à créer).")
    new_roles = sum(1 for op in b.ops if op.kind is OpKind.CREATE_ROLE)
    if new_roles and len(b.snapshot.roles) + new_roles > MAX_ROLES:
        blockers.append(f"Le serveur dépasserait la limite Discord de {MAX_ROLES} rôles.")
    # Verrouillage : au moins un salon textuel de la cible doit rester visible par @everyone.
    text_targets = [res.spec for res in report.channels
                    if TYPE_FAMILY[res.spec.type] == "text" and _wanted(res.spec, res.channel is not None, b)]
    if b.target.manages_permissions and b.options.manage_permissions and b.layout.policy.permissions and text_targets:
        if all(b.target.hides_from_everyone(spec) for spec in text_targets):
            blockers.append("Avec cette configuration, @everyone ne verrait plus aucun salon textuel : refonte bloquée pour "
                            "ne pas verrouiller le serveur. Vérifiez les niveaux d'accès de config/server.toml.")
    return blockers


# --- Analyse (première page de /refonte) ------------------------------------------------------


@dataclass(frozen=True)
class ServerAnalysis:
    categories: int
    text_channels: int
    voice_channels: int
    other_channels: int
    roles: int
    integration_roles: int
    roles_above_bot: tuple[str, ...]
    admin_roles: tuple[str, ...]
    duplicates: tuple[tuple[str, int], ...]
    missing_bot_permissions: tuple[str, ...]
    community: bool = False


def analyse(snapshot: GuildSnapshot) -> ServerAnalysis:
    families = Counter(TYPE_FAMILY.get(ch.type, "other") for ch in snapshot.channels)
    roles = [role for role in snapshot.roles if not role.is_default]
    missing = tuple(label for name, label in BOT_PERMISSION_LABELS.items()
                    if name not in snapshot.bot_permissions and "administrator" not in snapshot.bot_permissions)
    return ServerAnalysis(
        categories=len(snapshot.categories),
        text_channels=families.get("text", 0),
        voice_channels=families.get("voice", 0),
        other_channels=families.get("forum", 0) + families.get("other", 0),
        roles=len(roles),
        integration_roles=sum(1 for r in roles if r.managed),
        roles_above_bot=tuple(r.name for r in sorted(roles, key=lambda r: -r.position) if r.position > snapshot.bot_top_role_position),
        admin_roles=tuple(r.name for r in roles if "administrator" in r.dangerous and not r.managed),
        duplicates=tuple(_duplicates(snapshot.channels)),
        missing_bot_permissions=missing,
        community=snapshot.community,
    )


# --- Présentation du plan (textes, sans Discord) --------------------------------------------

SUBJECT_LABELS = {"category": "catégorie", "role": "rôle", "config": "configuration"} | {
    kind: f"salon {label}" for kind, label in TYPE_LABELS.items()
}


def _perm_list(names) -> str:
    return ", ".join(PERMISSION_LABELS.get(name, DANGEROUS_PERMISSIONS.get(name, name)) for name in names)


def _hex(color: int | None) -> str:
    return f"#{color:06X}" if color else "sans couleur"


def _color(color: int | None) -> str:
    return f"({_hex(color)})"


def target_tree(plan: RedesignPlan) -> list[str]:
    """La structure cible, catégorie par catégorie, telle qu'elle sera affichée."""
    if plan.target is None or plan.report is None:
        return []
    created_or_kept = {op.key for op in plan.operations if op.kind is not OpKind.SKIP and op.subject != "role"}
    lines = []
    for res in plan.report.categories:
        if res.spec.key not in created_or_kept:
            continue
        channels = [plan.target.name(spec) for spec in plan.target.channels_of(res.spec.key) if spec.key in created_or_kept]
        lines.append(f"{plan.target.name(res.spec)} : {', '.join(channels) if channels else '(vide)'}")
    roles = [f"{op.target} {_color(op.params.get('color'))}" if op.kind is OpKind.CREATE_ROLE else op.target
             for op in plan.operations if op.subject == "role" and op.kind in (OpKind.CREATE_ROLE, OpKind.EDIT_ROLE, OpKind.KEEP)]
    if roles:
        lines.append("Rôles : " + " > ".join(roles))
    return lines


def plan_sections(plan: RedesignPlan) -> list[tuple[str, list[str]]]:
    """Sections affichées dans l'aperçu, dans un ordre fixe ; les sections vides sont omises."""
    sections: list[tuple[str, list[str]]] = []
    tree = target_tree(plan)
    if tree:
        label = plan.target.profile.label if plan.target and plan.target.profile else "par défaut"
        sections.append((f"Structure cible · profil {label}, style {plan.target.style.label}", tree))
    if plan.blockers:
        sections.append((f"Bloquant ({len(plan.blockers)})", list(plan.blockers)))
    for kind in EXECUTION_ORDER:
        items = plan.of(kind)
        if items:
            sections.append((f"{KIND_LABELS[kind]} ({len(items)})", [describe(op) for op in items]))
    if plan.risks:
        sections.append((f"Risques détectés ({len(plan.risks)})", list(plan.risks)))
    for kind in (OpKind.MANUAL, OpKind.DUPLICATE, OpKind.CUSTOM, OpKind.UNKNOWN, OpKind.SKIP, OpKind.KEEP):
        items = plan.of(kind)
        if items:
            sections.append((f"{KIND_LABELS[kind]} ({len(items)})", [describe(op) for op in items]))
    sections.append(deletion_section(plan))
    return sections


def deletion_section(plan: RedesignPlan) -> tuple[str, list[str]]:
    if not plan.deletions:
        return ("Suppressions", ["Aucune suppression proposée : rien ne sera supprimé."])
    if plan.options.deletions:
        status = "sur confirmation séparée, après examen (bouton « Examiner les suppressions »)"
    else:
        status = "NON appliquées : relancez avec suppressions:True pour les examiner"
    return (f"Suppressions proposées ({len(plan.deletions)}) · {status}", [describe(op) for op in plan.deletions])


def describe(op: Operation) -> str:
    subject = SUBJECT_LABELS.get(op.subject, op.subject)
    text = f"{op.target} ({subject})"
    if op.changes:
        text += " : " + " ; ".join(op.changes)
        if op.expected:
            text += f" ({op.expected})"
    elif op.expected:
        text += f" : {op.expected}"
    return text


def summary_lines(plan: RedesignPlan) -> list[str]:
    """Récapitulatif chiffré des changements envisagés."""
    counts = plan.counts
    lines = [f"{counts[kind]} · {KIND_LABELS[kind].lower()}" for kind in EXECUTION_ORDER if counts[kind]]
    if not lines:
        lines.append("Aucune modification nécessaire : le serveur est déjà conforme.")
    lines.append(f"{len(plan.actions)} opération(s) au total")
    for kind in (OpKind.KEEP, OpKind.MANUAL, OpKind.DUPLICATE, OpKind.CUSTOM, OpKind.UNKNOWN, OpKind.SKIP):
        if counts[kind]:
            lines.append(f"{counts[kind]} · {KIND_LABELS[kind].lower()}")
    if plan.deletions:
        state = "à examiner séparément" if plan.options.deletions else "désactivées (option suppressions)"
        lines.append(f"{len(plan.deletions)} · suppression(s) proposée(s), {state}")
    else:
        lines.append("0 · suppression")
    return lines


# --- Résultat de l'application ----------------------------------------------------------------


class ResultStatus(StrEnum):
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class OperationResult:
    operation: Operation
    status: ResultStatus
    detail: str = ""
    new_id: int | None = None
    undo: dict = field(default_factory=dict)  # état d'avant, relu sur Discord juste avant la modification


@dataclass(frozen=True)
class RedesignOutcome:
    results: tuple[OperationResult, ...]
    manual: tuple[Operation, ...]
    backup: str | None = None  # nom du fichier de sauvegarde de la base
    journal: str | None = None  # nom du journal des opérations
    remaining: tuple[str, ...] = ()  # écarts encore constatés à la vérification finale
    verified: bool = False
    title: str = "Refonte"

    def with_status(self, status: ResultStatus) -> list[OperationResult]:
        return [r for r in self.results if r.status is status]

    @property
    def complete(self) -> bool:
        """Vrai seulement si aucune opération n'a échoué ni n'a été sautée."""
        return all(r.status is ResultStatus.DONE for r in self.results)


def outcome_sections(outcome: RedesignOutcome) -> list[tuple[str, list[str]]]:
    sections = []
    for status, title in (
        (ResultStatus.FAILED, "Échouées"),
        (ResultStatus.SKIPPED, "Ignorées pendant l'application"),
        (ResultStatus.DONE, "Réussies"),
    ):
        items = outcome.with_status(status)
        if items:
            sections.append((f"{title} ({len(items)})", [result_line(r) for r in items]))
    if outcome.verified:
        if outcome.remaining:
            sections.append((f"Vérification : écarts restants ({len(outcome.remaining)})", list(outcome.remaining)))
        else:
            sections.append(("Vérification", ["Serveur relu après application : conforme au plan."]))
    if outcome.manual:
        sections.append((f"À faire à la main ({len(outcome.manual)})", [describe(op) for op in outcome.manual]))
    return sections


RESULT_LABELS = {
    OpKind.CREATE_ROLE: "Création du rôle",
    OpKind.EDIT_ROLE: "Modification du rôle",
    OpKind.REORDER_ROLES: "Hiérarchie des rôles",
    OpKind.CREATE_CATEGORY: "Création de la catégorie",
    OpKind.EDIT_CATEGORY: "Renommage de la catégorie",
    OpKind.CREATE_CHANNEL: "Création du salon",
    OpKind.EDIT_CHANNEL: "Modification du salon",
    OpKind.MOVE_CHANNEL: "Déplacement du salon",
    OpKind.SET_PERMISSIONS: "Permissions de",
    OpKind.REORDER_CATEGORIES: "Ordre des catégories",
    OpKind.REORDER_CHANNELS: "Ordre des salons de",
    OpKind.BIND: "Association",
    OpKind.DELETE_CHANNEL: "Suppression du salon",
    OpKind.DELETE_CATEGORY: "Suppression de la catégorie",
    OpKind.DELETE_ROLE: "Suppression du rôle",
}


def result_line(result: OperationResult) -> str:
    op = result.operation
    label = f"{RESULT_LABELS.get(op.kind, op.kind)} « {op.target} »"
    return f"{label} : {result.detail}" if result.detail else label
