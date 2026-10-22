"""Retour arrière d'une refonte (/refonte retour), à partir de son journal, sans discord.py.

Le journal d'une refonte garde, pour chaque opération réussie, l'état relu sur
Discord juste avant la modification (``undo``). Le retour arrière en déduit un
plan inverse, en ordre inverse, qui ne touche que ce qui n'a pas changé depuis :

* renommages, sujets, types, déplacements, permissions, couleurs et positions :
  rétablis si l'élément est encore dans l'état laissé par la refonte ;
* éléments créés par la refonte : leur suppression est **proposée** (liste
  séparée, confirmation distincte), seulement s'ils sont vides ;
* éléments supprimés par la refonte : recréés vides (les messages, eux, sont
  perdus définitivement ; un rôle recréé n'est réattribué à personne).

Tout ce qui ne peut pas être rétabli sans risque est signalé, jamais forcé.
"""

from __future__ import annotations

from src.services.server_redesign import Operation, OpKind, RedesignOptions, RedesignPlan
from src.services.server_structure import GuildSnapshot, Overwrite

LIMITS = (
    "Les messages d'un salon supprimé ne reviennent jamais : un salon recréé est vide.",
    "Un rôle supprimé puis recréé n'est réattribué à personne.",
    "Les positions sont rétablies au mieux : Discord renumérote les éléments déplacés depuis.",
    "Ce qui a été modifié à la main depuis la refonte est laissé tel quel et signalé.",
)


def build_rollback_plan(journal: dict, snapshot: GuildSnapshot) -> RedesignPlan:
    channels = {ch.id: ch for ch in snapshot.channels}
    categories = {cat.id: cat for cat in snapshot.categories}
    roles = {role.id: role for role in snapshot.roles}
    ops: list[Operation] = []
    deletions: list[Operation] = []
    done = [r for r in journal.get("results", []) if r.get("status") == "done"]

    def manual(op: dict, reason: str) -> None:
        ops.append(Operation(OpKind.MANUAL, op["key"], op["subject"], op["target"], op.get("object_id"), expected=reason))

    for result in reversed(done):
        op, undo, kind = result["operation"], result.get("undo") or {}, OpKind(result["operation"]["kind"])
        if kind is OpKind.CREATE_ROLE:
            role = roles.get(result.get("new_id") or 0)
            if role is None:
                continue
            if role.position >= snapshot.bot_top_role_position:
                manual(op, "rôle créé par la refonte, désormais au-dessus de Samae : à supprimer à la main si besoin")
            else:
                deletions.append(Operation(OpKind.DELETE_ROLE, op["key"], "role", role.name, role.id, role.name,
                                           params={"id": role.id, "name": role.name},
                                           expected="créé par la refonte ; retiré aux membres qui l'ont reçu depuis"))
        elif kind is OpKind.CREATE_CHANNEL:
            channel = channels.get(result.get("new_id") or 0)
            if channel is None:
                continue
            if channel.has_messages:
                manual(op, "salon créé par la refonte mais qui contient des messages : conservé")
            else:
                deletions.append(Operation(OpKind.DELETE_CHANNEL, op["key"], channel.type, channel.name, channel.id, channel.name,
                                           params={"id": channel.id, "name": channel.name, "require_empty": True},
                                           expected="créé par la refonte, aucun message"))
        elif kind is OpKind.CREATE_CATEGORY:
            category = categories.get(result.get("new_id") or 0)
            if category is None:
                continue
            # Les salons déplacés dans la catégorie retournent d'abord à leur place (ordre inverse du journal).
            moving_back = {o.object_id for o in ops if o.kind is OpKind.MOVE_CHANNEL}
            content = [ch for ch in snapshot.channels if ch.category_id == category.id and ch.id not in moving_back]
            removable = {d.object_id for d in deletions}
            if all(ch.id in removable for ch in content):
                deletions.append(Operation(OpKind.DELETE_CATEGORY, op["key"], "category", category.name, category.id, category.name,
                                           params={"id": category.id, "name": category.name},
                                           expected="créée par la refonte" + (", vidée par les suppressions ci-dessus" if content else ", vide")))
            else:
                manual(op, "catégorie créée par la refonte qui contient d'autres salons : conservée")
        elif kind in (OpKind.EDIT_CHANNEL, OpKind.EDIT_CATEGORY):
            element = channels.get(undo.get("id") or 0) or categories.get(undo.get("id") or 0)
            if element is None:
                manual(op, "élément introuvable : rien à rétablir")
                continue
            if "name" in op["params"] and element.name != op["params"]["name"]:
                manual(op, f"renommé depuis la refonte (« {element.name} ») : laissé tel quel")
                continue
            params = {"name": undo["name"]} if element.name != undo["name"] else {}
            changes = [f"nom : « {element.name} » → « {undo['name']} »"] if params else []
            if "topic" in undo and getattr(element, "topic", "") == op["params"].get("topic"):
                params["topic"] = undo["topic"]
                changes.append("sujet d'origine rétabli")
            if "type" in undo and getattr(element, "type", "") == op["params"].get("type"):
                params["type"] = undo["type"]
                changes.append("type d'origine rétabli")
            if params:
                ops.append(Operation(kind, op["key"], op["subject"], undo["name"], element.id, element.name,
                                     params=params, changes=tuple(changes), expected="état d'avant la refonte"))
        elif kind is OpKind.MOVE_CHANNEL:
            channel = channels.get(undo.get("id") or 0)
            old = undo.get("category_id")
            if channel is None:
                continue
            if old is None or old not in categories:
                manual(op, "l'ancienne catégorie n'existe plus (ou le salon n'en avait pas) : à déplacer à la main")
                continue
            if channel.category_id != old:
                ops.append(Operation(OpKind.MOVE_CHANNEL, op["key"], channel.type, channel.name, channel.id, channel.name,
                                     params={"category": f"id:{old}"}, changes=(f"retour dans « {categories[old].name} »",),
                                     expected="permissions du salon inchangées par le déplacement"))
        elif kind is OpKind.SET_PERMISSIONS:
            element = channels.get(undo.get("id") or 0) or categories.get(undo.get("id") or 0)
            if element is None:
                continue
            current = {(o.target_type, o.target_id): o for o in element.overwrites}
            set_values = {}
            for target, (target_type, target_id) in op["params"].get("ids", {}).items():
                if target_id is not None:
                    set_values[f"{'rid' if target_type == 'role' else 'mid'}:{target_id}"] = op["params"]["targets"][target]
            changed_since = [ref for ref, rule in set_values.items() if not _matches(current, ref, rule)]
            if changed_since:
                manual(op, "permissions modifiées depuis la refonte : laissées telles quelles")
                continue
            targets = {ref: (rule or {"allow": [], "deny": []}) for ref, rule in undo.get("overwrites", {}).items()}
            ops.append(Operation(OpKind.SET_PERMISSIONS, op["key"], op["subject"], element.name, element.id,
                                 params={"targets": targets}, changes=("permissions d'avant la refonte rétablies",),
                                 expected="seules les cibles modifiées par la refonte changent"))
        elif kind is OpKind.EDIT_ROLE:
            role = roles.get(undo.get("id") or 0)
            if role is None:
                manual(op, "rôle introuvable : rien à rétablir")
                continue
            p = op["params"]
            if ("name" in p and role.name != p["name"]) or ("color" in p and role.color != p["color"]):
                manual(op, f"rôle « {role.name} » modifié depuis la refonte : laissé tel quel")
                continue
            params = {"name": undo["name"], "color": undo["color"], "hoist": undo["hoist"], "mentionable": undo["mentionable"]}
            added = [n for n in p.get("add_permissions", []) if n not in undo.get("permissions", [])]
            if added:
                params["remove_permissions"] = added
            ops.append(Operation(OpKind.EDIT_ROLE, op["key"], "role", undo["name"], role.id, role.name, params=params,
                                 changes=("nom, couleur, affichage et permissions d'avant la refonte",), expected="état d'avant la refonte"))
        elif kind in (OpKind.REORDER_ROLES, OpKind.REORDER_CATEGORIES, OpKind.REORDER_CHANNELS):
            positions = undo.get("positions") or {}
            if positions:
                ops.append(Operation(kind, op["key"], op["subject"], op["target"], params={"positions": positions},
                                     changes=(f"{len(positions)} position(s) d'avant la refonte",), expected="rétabli au mieux"))
        elif kind in (OpKind.DELETE_CHANNEL, OpKind.DELETE_CATEGORY):
            parent = undo.get("category_id")
            if undo.get("deleted") == "category":
                ops.append(Operation(OpKind.CREATE_CATEGORY, op["key"], "category", undo["name"],
                                     params={"name": undo["name"], "overwrites": undo.get("overwrites", {})},
                                     expected="recréée vide"))
            elif parent is not None and parent not in categories:
                manual(op, f"salon supprimé « {undo['name']} » : sa catégorie n'existe plus, à recréer à la main")
            else:
                ops.append(Operation(OpKind.CREATE_CHANNEL, op["key"], undo["deleted"], undo["name"],
                                     params={"name": undo["name"], "type": undo["deleted"], "topic": undo.get("topic", ""),
                                             "category": f"id:{parent}" if parent else "", "overwrites": undo.get("overwrites", {})},
                                     expected="recréé VIDE : les messages supprimés sont perdus définitivement"))
        elif kind is OpKind.DELETE_ROLE:
            ops.append(Operation(OpKind.CREATE_ROLE, op["key"], "role", undo["name"],
                                 params={"name": undo["name"], "color": undo.get("color"), "hoist": undo.get("hoist", False),
                                         "mentionable": undo.get("mentionable", False), "permissions": undo.get("permissions", [])},
                                 expected="recréé : il n'est réattribué à personne"))
    before = journal.get("before", {})
    if before.get("bindings") is not None:
        existing = {ch.id for ch in snapshot.channels}
        bindings = {k: v for k, v in before["bindings"].items() if v in existing}
        ops.append(Operation(OpKind.BIND, "bindings", "config", "associations de Samae", params={"bindings": bindings},
                             expected=f"{len(bindings)} association(s) d'avant la refonte rétablie(s)"))
    return RedesignPlan(tuple(ops), RedesignOptions(deletions=True), risks=LIMITS, deletions=tuple(deletions))


def _matches(current: dict, ref: str, rule: dict) -> bool:
    prefix, _, value = ref.partition(":")
    found: Overwrite | None = current.get(("role" if prefix == "rid" else "member", int(value)))
    allow, deny = set(rule.get("allow", [])), set(rule.get("deny", []))
    if found is None:
        return not allow and not deny
    return set(found.allow) == allow and set(found.deny) == deny
