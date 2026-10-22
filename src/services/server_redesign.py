"""Refonte du serveur (/refonte) : analyse et plan, sans dépendance à discord.py.

Le plan est calculé uniquement à partir d'un instantané du serveur
(``GuildSnapshot``), de la structure cible (``config/server.toml``) et des
associations déjà enregistrées. Il est **déterministe** : le même état et la même
cible donnent les mêmes opérations, dans le même ordre, avec la même empreinte.
L'empreinte permet de vérifier, au moment de confirmer, que le serveur n'a pas
changé depuis l'aperçu.

La refonte est **non destructive** : aucune opération ne supprime de salon, de
catégorie, de rôle ou de message, et aucune ne modifie un rôle existant ni les
permissions d'un salon existant. Les seules permissions posées le sont sur les
catégories privées *créées* par la refonte (et leurs nouveaux salons).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from src.config import ChannelSpec, ServerLayout
from src.services.server_structure import (
    ChannelStatus,
    GuildChannel,
    GuildSnapshot,
    StructureReport,
    normalize_name,
    resolve_structure,
)

# Limites de Discord : 500 salons (catégories comprises) par serveur, 50 salons par catégorie.
MAX_GUILD_CHANNELS = 500
MAX_CATEGORY_CHANNELS = 50
# Permissions sensibles relevées dans l'analyse des rôles (jamais modifiées par la refonte).
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
PERMISSION_LABELS = {
    "manage_channels": "Gérer les salons",
    "manage_roles": "Gérer les rôles",
    "view_channel": "Voir les salons",
}


class OpKind(StrEnum):
    CREATE_CATEGORY = "create_category"
    RENAME_CATEGORY = "rename_category"
    CREATE_CHANNEL = "create_channel"
    RENAME_CHANNEL = "rename_channel"
    MOVE_CHANNEL = "move_channel"
    CREATE_ROLE = "create_role"
    BIND = "bind"  # association « fonction de Samae → salon » enregistrée en base
    KEEP = "keep"  # déjà conforme : rien à faire
    SKIP = "skip"  # volontairement non traité (facultatif, option non choisie…)
    MANUAL = "manual"  # nécessite une intervention humaine


ACTIONABLE = {
    OpKind.CREATE_CATEGORY,
    OpKind.RENAME_CATEGORY,
    OpKind.CREATE_CHANNEL,
    OpKind.RENAME_CHANNEL,
    OpKind.MOVE_CHANNEL,
    OpKind.CREATE_ROLE,
    OpKind.BIND,
}
# Ordre d'exécution : catégories d'abord (les salons s'y rangent), configuration en dernier.
EXECUTION_ORDER = [
    OpKind.CREATE_CATEGORY,
    OpKind.RENAME_CATEGORY,
    OpKind.CREATE_CHANNEL,
    OpKind.RENAME_CHANNEL,
    OpKind.MOVE_CHANNEL,
    OpKind.CREATE_ROLE,
    OpKind.BIND,
]
KIND_LABELS = {
    OpKind.CREATE_CATEGORY: "Catégories à créer",
    OpKind.RENAME_CATEGORY: "Catégories à renommer",
    OpKind.CREATE_CHANNEL: "Salons à créer",
    OpKind.RENAME_CHANNEL: "Salons à renommer",
    OpKind.MOVE_CHANNEL: "Salons à déplacer",
    OpKind.CREATE_ROLE: "Rôles à créer",
    OpKind.BIND: "Configuration de Samae",
    OpKind.KEEP: "Conservés sans changement",
    OpKind.SKIP: "Ignorés",
    OpKind.MANUAL: "Interventions manuelles",
}


@dataclass(frozen=True)
class Operation:
    kind: OpKind
    key: str  # clé de config/server.toml (ou du rôle)
    subject: str  # "category", "text", "voice", "role" ou "config"
    target: str  # nom cible (ou salon visé par une association)
    object_id: int | None = None  # identifiant Discord de l'élément existant
    current: str = ""  # nom actuel, ou catégorie actuelle pour un déplacement
    category_key: str | None = None  # catégorie cible (création ou déplacement)
    category_id: int | None = None  # identifiant de la catégorie cible si elle existe déjà
    private: bool = False  # création visible seulement du bot et des administrateurs
    expected: str = ""  # résultat attendu, ou raison (ignoré, manuel)

    @property
    def actionable(self) -> bool:
        return self.kind in ACTIONABLE


@dataclass(frozen=True)
class RedesignOptions:
    create_optional: bool = False  # créer aussi les salons facultatifs absents
    create_roles: bool = False  # créer les rôles de projet absents (sans permission)


@dataclass(frozen=True)
class RedesignPlan:
    operations: tuple[Operation, ...]
    options: RedesignOptions
    blockers: tuple[str, ...] = ()  # empêchent l'application (permissions du bot, limites)
    risks: tuple[str, ...] = ()  # points d'attention, sans effet sur l'application
    untouched: tuple[str, ...] = ()  # salons hors de la structure cible, conservés tels quels
    report: StructureReport | None = field(default=None, compare=False)

    def of(self, *kinds: OpKind) -> list[Operation]:
        return [op for op in self.operations if op.kind in kinds]

    @property
    def actions(self) -> list[Operation]:
        """Opérations à exécuter, dans l'ordre d'exécution."""
        return sorted((op for op in self.operations if op.actionable), key=lambda op: EXECUTION_ORDER.index(op.kind))

    @property
    def counts(self) -> dict[OpKind, int]:
        counter = Counter(op.kind for op in self.operations)
        return {kind: counter.get(kind, 0) for kind in OpKind}

    @property
    def needs_changes(self) -> bool:
        return bool(self.actions)

    @property
    def can_apply(self) -> bool:
        return self.needs_changes and not self.blockers

    @property
    def fingerprint(self) -> str:
        """Empreinte des opérations à exécuter : change dès que le serveur ou la cible changent."""
        data = [asdict(op) for op in self.actions] + [asdict(self.options), list(self.blockers)]
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def required_bot_permissions(plan_ops: list[Operation]) -> dict[str, str]:
    needed: dict[str, str] = {}
    if any(op.subject in {"category", "text", "voice"} for op in plan_ops if op.actionable and op.kind is not OpKind.BIND):
        needed["manage_channels"] = PERMISSION_LABELS["manage_channels"]
    if any(op.kind is OpKind.CREATE_ROLE for op in plan_ops):
        needed["manage_roles"] = PERMISSION_LABELS["manage_roles"]
    return needed


def build_plan(
    layout: ServerLayout,
    snapshot: GuildSnapshot,
    *,
    saved_bindings: dict[str, int] | None = None,
    saved_categories: dict[str, int] | None = None,
    options: RedesignOptions = RedesignOptions(),
    alerts_channel: str | None = None,
) -> RedesignPlan:
    """Plan de refonte : uniquement de la lecture, aucun effet de bord."""
    saved_bindings = dict(saved_bindings or {})
    report = resolve_structure(layout, snapshot, saved_bindings, saved_categories)
    ops: list[Operation] = []
    risks: list[str] = []
    channel_res = {res.spec.key: res for res in report.channels}
    category_res = {res.spec.key: res for res in report.categories}
    categories_by_id = {cat.id: cat for cat in snapshot.categories}

    # Salons voulus : tous les salons non facultatifs, plus les facultatifs qui existent déjà ou demandés.
    def wanted(spec: ChannelSpec) -> bool:
        res = channel_res[spec.key]
        return not spec.optional or options.create_optional or res.channel is not None

    needed_categories = {spec.category_key for spec in layout.channels.values() if wanted(spec)}

    # --- Catégories --------------------------------------------------------------------
    category_ok: dict[str, bool] = {}  # la catégorie existera-t-elle après la refonte ?
    for spec in layout.categories.values():
        res = category_res[spec.key]
        cat = res.category
        if cat is not None:
            category_ok[spec.key] = True
            if cat.name != spec.name:
                if cat.manageable:
                    ops.append(Operation(OpKind.RENAME_CATEGORY, spec.key, "category", spec.name, cat.id, cat.name,
                                         expected=f"« {cat.name} » devient « {spec.name} »"))
                else:
                    ops.append(Operation(OpKind.MANUAL, spec.key, "category", spec.name, cat.id, cat.name,
                                         expected="le bot ne peut pas modifier cette catégorie (« Gérer les salons » refusé)"))
            else:
                ops.append(Operation(OpKind.KEEP, spec.key, "category", spec.name, cat.id, cat.name, expected="déjà conforme"))
            if spec.private and cat.everyone_can_view:
                risks.append(
                    f"La catégorie « {cat.name} » doit être privée mais @everyone peut la voir : Samae ne modifie pas "
                    "les permissions d'une catégorie existante, restreignez-la à la main."
                )
        elif res.ambiguous:
            category_ok[spec.key] = False
            ops.append(Operation(OpKind.MANUAL, spec.key, "category", spec.name,
                                 expected=f"{res.note} : renommez ou regroupez les doublons, puis relancez l'analyse"))
        elif spec.key in needed_categories:
            category_ok[spec.key] = True
            ops.append(Operation(OpKind.CREATE_CATEGORY, spec.key, "category", spec.name, private=spec.private,
                                 expected="nouvelle catégorie privée (bot et administrateurs)" if spec.private else "nouvelle catégorie"))
        else:
            category_ok[spec.key] = False
            ops.append(Operation(OpKind.SKIP, spec.key, "category", spec.name,
                                 expected="aucun salon à y ranger (salons facultatifs non demandés)"))

    def category_target(spec: ChannelSpec) -> tuple[int | None, str]:
        res = category_res[spec.category_key]
        return (res.category.id if res.category else None), layout.categories[spec.category_key].name

    # Noms déjà pris, pour ne jamais créer ou renommer vers un doublon.
    taken = Counter((normalize_name(ch.name), ch.type) for ch in snapshot.channels)

    # --- Salons ------------------------------------------------------------------------
    incoming: Counter[str] = Counter()  # salons ajoutés à chaque catégorie (limite de 50)
    for spec in layout.channels.values():
        res = channel_res[spec.key]
        cat_id, cat_name = category_target(spec)
        private = layout.categories[spec.category_key].private
        if res.usable and res.channel is not None:
            ch = res.channel
            channel_ops: list[Operation] = []
            if ch.name != spec.name:
                clash = normalize_name(spec.name) != normalize_name(ch.name) and taken[(normalize_name(spec.name), ch.type)] > 0
                if not ch.manageable:
                    channel_ops.append(_manual(spec, ch, "le bot ne peut pas renommer ce salon (« Gérer les salons » refusé dans le salon)"))
                elif clash:
                    channel_ops.append(_manual(spec, ch, f"un autre salon s'appelle déjà « {spec.name} » : renommage annulé pour éviter un doublon"))
                else:
                    channel_ops.append(Operation(OpKind.RENAME_CHANNEL, spec.key, spec.type, spec.name, ch.id, ch.name,
                                                 expected=f"« {ch.name} » devient « {spec.name} » (historique et identifiant conservés)"))
            if ch.category_id != cat_id or cat_id is None:
                current_cat = categories_by_id.get(ch.category_id or 0)
                current_label = current_cat.name if current_cat else "hors catégorie"
                if not category_ok.get(spec.category_key):
                    channel_ops.append(_manual(spec, ch, f"catégorie « {cat_name} » indisponible : salon laissé dans « {current_label} »"))
                elif not ch.manageable:
                    channel_ops.append(_manual(spec, ch, "le bot ne peut pas déplacer ce salon (« Gérer les salons » refusé dans le salon)"))
                else:
                    incoming[spec.category_key] += 1
                    channel_ops.append(Operation(OpKind.MOVE_CHANNEL, spec.key, spec.type, spec.name, ch.id, current_label,
                                                 category_key=spec.category_key, category_id=cat_id,
                                                 expected=f"de « {current_label} » vers « {cat_name} » (permissions du salon inchangées)"))
            ops.extend(channel_ops or [Operation(OpKind.KEEP, spec.key, spec.type, spec.name, ch.id, ch.name, expected="déjà conforme")])
        elif res.status is ChannelStatus.MISSING:
            if not wanted(spec):
                ops.append(Operation(OpKind.SKIP, spec.key, spec.type, spec.name,
                                     expected="salon facultatif : créé seulement avec l'option facultatifs:True"))
            elif not category_ok.get(spec.category_key):
                ops.append(Operation(OpKind.MANUAL, spec.key, spec.type, spec.name,
                                     expected=f"catégorie « {cat_name} » indisponible : salon non créé"))
            elif taken[(normalize_name(spec.name), spec.type)]:
                ops.append(Operation(OpKind.MANUAL, spec.key, spec.type, spec.name,
                                     expected="un salon de ce nom existe déjà mais ne peut pas être associé : vérifiez les doublons"))
            else:
                incoming[spec.category_key] += 1
                ops.append(Operation(OpKind.CREATE_CHANNEL, spec.key, spec.type, spec.name, category_key=spec.category_key,
                                     category_id=cat_id, private=private,
                                     expected=f"nouveau salon {'vocal' if spec.type == 'voice' else 'textuel'} dans « {cat_name} »"
                                     + (" (privé)" if private else "")))
                taken[(normalize_name(spec.name), spec.type)] += 1
        else:  # AMBIGUOUS ou WRONG_TYPE : jamais de choix au hasard, jamais de doublon créé
            ops.append(Operation(OpKind.MANUAL, spec.key, spec.type, spec.name,
                                 res.channel.id if res.channel else None, res.channel.name if res.channel else "",
                                 expected=f"{res.note} : à régler à la main, puis relancez l'analyse"))

    # Limite de 50 salons par catégorie.
    for key, count in incoming.items():
        res = category_res[key]
        existing = sum(1 for ch in snapshot.channels if res.category and ch.category_id == res.category.id)
        if existing + count > MAX_CATEGORY_CHANNELS:
            risks.append(f"La catégorie « {layout.categories[key].name} » dépasserait {MAX_CATEGORY_CHANNELS} salons : Discord refusera une partie des opérations.")

    # --- Rôles -------------------------------------------------------------------------
    roles_by_name = {normalize_name(role.name): role for role in snapshot.roles}
    for spec in layout.roles.values():
        role = roles_by_name.get(normalize_name(spec.name))
        if role is not None:
            ops.append(Operation(OpKind.KEEP, spec.key, "role", spec.name, role.id, role.name, expected="rôle existant, jamais modifié"))
        elif options.create_roles:
            ops.append(Operation(OpKind.CREATE_ROLE, spec.key, "role", spec.name,
                                 expected="nouveau rôle sans aucune permission, non mentionnable"))
        else:
            ops.append(Operation(OpKind.SKIP, spec.key, "role", spec.name, expected="créé seulement avec l'option roles:True"))

    # --- Configuration de Samae (associations enregistrées) --------------------------------
    created_keys = {op.key for op in ops if op.kind is OpKind.CREATE_CHANNEL}
    for spec in layout.channels.values():
        res = channel_res[spec.key]
        saved = saved_bindings.get(spec.key)
        if spec.key in created_keys:
            ops.append(Operation(OpKind.BIND, spec.key, "config", spec.name,
                                 expected=f"fonction « {spec.key} » associée au nouveau salon « {spec.name} »"))
        elif res.usable and res.channel is not None and saved != res.channel.id:
            ops.append(Operation(OpKind.BIND, spec.key, "config", spec.name, res.channel.id, str(saved or ""),
                                 expected=f"fonction « {spec.key} » associée au salon existant « {res.channel.name} » (par identifiant)"))
        elif saved is not None and not res.usable:
            ops.append(Operation(OpKind.BIND, spec.key, "config", spec.name, None, str(saved),
                                 expected=f"association « {spec.key} » retirée : le salon enregistré est introuvable"))
    if "logs" in layout.channels and alerts_channel != "logs":
        logs_ready = channel_res["logs"].usable or "logs" in created_keys
        if logs_ready:
            current = f"ALERTS_CHANNEL={alerts_channel}" if alerts_channel else "ALERTS_CHANNEL vide"
            ops.append(Operation(OpKind.MANUAL, "alerts_channel", "config", "logs",
                                 expected=f"{current} : pour recevoir les alertes dans #bot-logs, mettez ALERTS_CHANNEL=logs dans .env puis redémarrez"))

    # --- Catégories hors cible qui se retrouveraient vides --------------------------------
    target_category_ids = {res.category.id for res in report.categories if res.category}
    moved_out = Counter(
        next((ch.category_id for ch in snapshot.channels if ch.id == op.object_id), None)
        for op in ops if op.kind is OpKind.MOVE_CHANNEL
    )
    for cat in sorted(snapshot.categories, key=lambda c: (c.position, c.id)):
        if cat.id in target_category_ids:
            continue
        content = sum(1 for ch in snapshot.channels if ch.category_id == cat.id)
        if content and content == moved_out.get(cat.id, 0):
            ops.append(Operation(OpKind.MANUAL, f"category:{cat.id}", "category", cat.name, cat.id, cat.name,
                                 expected="sera vide après la refonte : conservée (jamais supprimée) ; supprimez-la à la main si elle ne sert plus"))

    # --- Analyse complémentaire ----------------------------------------------------------
    duplicates = _duplicates(snapshot.channels)
    for name, count in duplicates:
        risks.append(f"{count} salons s'appellent « {name} » : Samae ne choisit jamais au hasard entre eux.")
    admins = [role.name for role in snapshot.roles if "administrator" in role.dangerous and not role.managed and not role.is_default]
    if len(admins) > 2:
        risks.append(f"{len(admins)} rôles ont la permission Administrateur ({', '.join(admins[:5])}) : vérifiez que c'est voulu (non modifié).")
    if any(op.kind is OpKind.RENAME_CHANNEL for op in ops):
        risks.append(
            "Des messages écrits à la main peuvent encore citer les anciens noms de salons ; les fonctions de Samae "
            "(messages permanents, réunions, notifications) utilisent les identifiants et continuent de fonctionner."
        )

    matched_ids = {res.channel.id for res in report.channels if res.channel}
    untouched = tuple(
        ch.name for ch in sorted(snapshot.channels, key=lambda c: (c.category_id or 0, c.position, c.id)) if ch.id not in matched_ids
    )

    plan_ops = tuple(ops)
    blockers = list(_blockers(plan_ops, snapshot))
    return RedesignPlan(plan_ops, options, tuple(blockers), tuple(dict.fromkeys(risks)), untouched, report)


def _manual(spec: ChannelSpec, channel: GuildChannel, reason: str) -> Operation:
    return Operation(OpKind.MANUAL, spec.key, spec.type, spec.name, channel.id, channel.name, expected=reason)


def _duplicates(channels: tuple[GuildChannel, ...]) -> list[tuple[str, int]]:
    counter = Counter((normalize_name(ch.name), ch.type) for ch in channels)
    names = {}
    for ch in channels:
        names.setdefault((normalize_name(ch.name), ch.type), ch.name)
    return sorted((names[key], count) for key, count in counter.items() if count > 1 and key[0])


def _blockers(ops: tuple[Operation, ...], snapshot: GuildSnapshot) -> list[str]:
    blockers = []
    for name, label in required_bot_permissions(list(ops)).items():
        if name not in snapshot.bot_permissions and "administrator" not in snapshot.bot_permissions:
            blockers.append(
                f"Samae n'a pas la permission « {label} » : Paramètres du serveur > Rôles > rôle du bot > cochez « {label} »."
            )
    created = sum(1 for op in ops if op.kind in (OpKind.CREATE_CATEGORY, OpKind.CREATE_CHANNEL))
    total = len(snapshot.categories) + len(snapshot.channels)
    if created and total + created > MAX_GUILD_CHANNELS:
        blockers.append(f"Le serveur dépasserait la limite Discord de {MAX_GUILD_CHANNELS} salons ({total} existants, {created} à créer).")
    return blockers


# --- Analyse (première page de /refonte) --------------------------------------------------


@dataclass(frozen=True)
class ServerAnalysis:
    categories: int
    text_channels: int
    voice_channels: int
    other_channels: int
    roles: int
    roles_above_bot: tuple[str, ...]
    admin_roles: tuple[str, ...]
    duplicates: tuple[tuple[str, int], ...]
    missing_bot_permissions: tuple[str, ...]


def analyse(snapshot: GuildSnapshot) -> ServerAnalysis:
    types = Counter(ch.type for ch in snapshot.channels)
    roles = [role for role in snapshot.roles if not role.is_default]
    missing = tuple(label for name, label in PERMISSION_LABELS.items()
                    if name not in snapshot.bot_permissions and "administrator" not in snapshot.bot_permissions)
    return ServerAnalysis(
        categories=len(snapshot.categories),
        text_channels=types.get("text", 0),
        voice_channels=types.get("voice", 0),
        other_channels=types.get("other", 0),
        roles=len(roles),
        roles_above_bot=tuple(r.name for r in sorted(roles, key=lambda r: -r.position) if r.position > snapshot.bot_top_role_position),
        admin_roles=tuple(r.name for r in roles if "administrator" in r.dangerous and not r.managed),
        duplicates=tuple(_duplicates(snapshot.channels)),
        missing_bot_permissions=missing,
    )


# --- Présentation du plan (textes, sans Discord) --------------------------------------------

SUBJECT_LABELS = {"category": "catégorie", "text": "salon textuel", "voice": "salon vocal", "role": "rôle", "config": "configuration"}


def plan_sections(plan: RedesignPlan) -> list[tuple[str, list[str]]]:
    """Sections affichées dans l'aperçu, dans un ordre fixe ; les sections vides sont omises."""
    sections: list[tuple[str, list[str]]] = []
    for kind in EXECUTION_ORDER:
        items = plan.of(kind)
        if items:
            sections.append((f"{KIND_LABELS[kind]} ({len(items)})", [describe(op) for op in items]))
    permissions = [describe_permissions(op) for op in plan.operations if op.private and op.kind in (OpKind.CREATE_CATEGORY, OpKind.CREATE_CHANNEL)]
    if permissions:
        sections.append((f"Permissions posées ({len(permissions)})", permissions))
    if plan.blockers:
        sections.append((f"Bloquant ({len(plan.blockers)})", list(plan.blockers)))
    if plan.risks:
        sections.append((f"Risques détectés ({len(plan.risks)})", list(plan.risks)))
    for kind in (OpKind.MANUAL, OpKind.SKIP, OpKind.KEEP):
        items = plan.of(kind)
        if items:
            sections.append((f"{KIND_LABELS[kind]} ({len(items)})", [describe(op) for op in items]))
    if plan.untouched:
        sections.append(
            (f"Hors de la structure cible ({len(plan.untouched)})", [f"{name} : conservé, non modifié" for name in plan.untouched])
        )
    return sections


def describe(op: Operation) -> str:
    subject = SUBJECT_LABELS.get(op.subject, op.subject)
    return f"{op.target} ({subject}) : {op.expected}" if op.expected else f"{op.target} ({subject})"


def describe_permissions(op: Operation) -> str:
    return (
        f"{op.target} : @everyone ne voit pas ; Samae et le rôle d'administration du bot (ADMIN_ROLE_NAME) "
        "y ont accès ; les administrateurs Discord aussi"
    )


def summary_lines(plan: RedesignPlan) -> list[str]:
    """Récapitulatif chiffré des changements envisagés."""
    counts = plan.counts
    lines = [
        f"{counts[kind]} · {KIND_LABELS[kind].lower()}" for kind in EXECUTION_ORDER if counts[kind]
    ]
    if not lines:
        lines.append("Aucune modification nécessaire : le serveur est déjà conforme.")
    lines.append(f"{counts[OpKind.KEEP]} · conservés sans changement")
    if counts[OpKind.MANUAL]:
        lines.append(f"{counts[OpKind.MANUAL]} · interventions manuelles")
    if counts[OpKind.SKIP]:
        lines.append(f"{counts[OpKind.SKIP]} · ignorés")
    lines.append("0 · suppression (la refonte ne supprime jamais rien)")
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


@dataclass(frozen=True)
class RedesignOutcome:
    results: tuple[OperationResult, ...]
    manual: tuple[Operation, ...]
    backup: str | None = None  # nom du fichier de sauvegarde de la base
    journal: str | None = None  # nom du journal des opérations

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
            sections.append((f"{title} ({len(items)})", [_result_line(r) for r in items]))
    if outcome.manual:
        sections.append((f"Interventions manuelles ({len(outcome.manual)})", [describe(op) for op in outcome.manual]))
    return sections


RESULT_LABELS = {
    OpKind.CREATE_CATEGORY: "Création de la catégorie",
    OpKind.RENAME_CATEGORY: "Renommage de la catégorie",
    OpKind.CREATE_CHANNEL: "Création du salon",
    OpKind.RENAME_CHANNEL: "Renommage du salon",
    OpKind.MOVE_CHANNEL: "Déplacement du salon",
    OpKind.CREATE_ROLE: "Création du rôle",
    OpKind.BIND: "Association",
}


def _result_line(result: OperationResult) -> str:
    op = result.operation
    label = f"{RESULT_LABELS.get(op.kind, op.kind)} « {op.target} »"
    return f"{label} : {result.detail}" if result.detail else label
