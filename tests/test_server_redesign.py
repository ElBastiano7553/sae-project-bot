"""Plan de /refonte (services.server_redesign) : lecture seule, déterministe, non destructif."""

from __future__ import annotations

import random

from src.config import load_server_layout
from src.services.server_redesign import (
    ACTIONABLE,
    OpKind,
    RedesignOptions,
    analyse,
    build_plan,
    plan_sections,
    summary_lines,
)
from src.services.server_structure import GuildCategory, GuildChannel, GuildRole, GuildSnapshot

LAYOUT = load_server_layout()
BOT_PERMS = frozenset({"manage_channels", "view_channel"})

# Le serveur tel que décrit par l'ancienne version de config/server.toml (V1 à 1.1.0).
OLD_CATEGORIES = [
    "01 · ACCUEIL & ORGANISATION", "02 · CONCEPTION DU JEU", "03 · DÉVELOPPEMENT",
    "04 · GITLAB AUTOMATISÉ", "05 · LIVRABLES SAE", "06 · VOCAL",
]
OLD_CHANNELS = [
    ("bienvenue", 0), ("annonces", 0), ("planning-et-deadlines", 0), ("discussion-generale", 0),
    ("decisions-et-comptes-rendus", 0), ("idees-et-game-design", 1), ("gameplay-et-ia", 1), ("assets-et-audio", 1),
    ("developpement-python", 2), ("reseau-multijoueur", 2), ("bugs-et-tests", 2), ("code-review", 2),
    ("gitlab-activite", 3), ("builds-et-tests", 3), ("bilan-ia", 3), ("documentation-et-preuves", 4),
    ("preparation-revues", 4),
]


def old_server(**extra) -> GuildSnapshot:
    categories = tuple(GuildCategory(10 + i, name, i) for i, name in enumerate(OLD_CATEGORIES))
    channels = [GuildChannel(100 + i, name, "text", 10 + cat, i) for i, (name, cat) in enumerate(OLD_CHANNELS)]
    channels += [GuildChannel(200, "reunion-equipe", "voice", 15), GuildChannel(201, "travail-en-commun", "voice", 15)]
    roles = (GuildRole(1, "@everyone", 0, is_default=True), GuildRole(2, "Samae", 5, managed=True))
    data = dict(categories=categories, channels=tuple(channels), roles=roles, bot_permissions=BOT_PERMS, bot_top_role_position=5)
    data.update(extra)
    return GuildSnapshot(**data)


def apply_in_memory(plan, snapshot: GuildSnapshot) -> tuple[GuildSnapshot, dict[str, int], dict[str, int]]:
    """Simule l'effet du plan sur l'instantané (ce que ferait Discord), pour vérifier l'idempotence."""
    categories = {c.id: c for c in snapshot.categories}
    channels = {c.id: c for c in snapshot.channels}
    roles = list(snapshot.roles)
    cat_ids = dict(plan.report.category_bindings)
    created: dict[str, int] = {}
    next_id = 5000
    for op in plan.actions:
        if op.kind is OpKind.CREATE_CATEGORY:
            categories[next_id] = GuildCategory(next_id, op.target, everyone_can_view=not op.private)
            cat_ids[op.key] = next_id
        elif op.kind is OpKind.RENAME_CATEGORY:
            categories[op.object_id] = GuildCategory(op.object_id, op.target)
        elif op.kind is OpKind.CREATE_CHANNEL:
            channels[next_id] = GuildChannel(next_id, op.target, op.subject, cat_ids[op.category_key])
            created[op.key] = next_id
        elif op.kind is OpKind.RENAME_CHANNEL:
            ch = channels[op.object_id]
            channels[op.object_id] = GuildChannel(ch.id, op.target, ch.type, ch.category_id)
        elif op.kind is OpKind.MOVE_CHANNEL:
            ch = channels[op.object_id]
            channels[op.object_id] = GuildChannel(ch.id, ch.name, ch.type, cat_ids[op.category_key])
        elif op.kind is OpKind.CREATE_ROLE:
            roles.append(GuildRole(next_id, op.target, 1))
        next_id += 1
    bindings = {**plan.report.bindings, **created}
    after = GuildSnapshot(tuple(categories.values()), tuple(channels.values()), tuple(roles), snapshot.bot_permissions, 5)
    return after, bindings, cat_ids


def keys(plan, kind):
    return [op.key for op in plan.of(kind)]


def test_old_server_plan_renames_and_moves_instead_of_recreating():
    plan = build_plan(LAYOUT, old_server())
    assert set(keys(plan, OpKind.CREATE_CATEGORY)) == {"commun", "organisation", "administration"}
    assert set(keys(plan, OpKind.RENAME_CATEGORY)) == {"accueil", "conception", "developpement", "livrables", "vocal"}
    # Seuls les salons vraiment nouveaux sont créés ; tous les autres sont renommés ou déplacés.
    assert set(keys(plan, OpKind.CREATE_CHANNEL)) == {"reunions", "comptes_rendus", "logs"}
    renamed = {op.current: op.target for op in plan.of(OpKind.RENAME_CHANNEL)}
    assert renamed["decisions-et-comptes-rendus"] == "décisions"
    assert renamed["gitlab-activite"] == "gitlab-et-commits"
    assert renamed["discussion-generale"] == "général"
    assert set(keys(plan, OpKind.MOVE_CHANNEL)) == {"discussion", "decisions", "planning", "builds", "gitlab_activite", "bilan_ia"}
    assert plan.can_apply and not plan.blockers


def test_plan_never_contains_a_deletion():
    plan = build_plan(LAYOUT, old_server(), options=RedesignOptions(create_optional=True, create_roles=True))
    assert {op.kind for op in plan.operations} <= set(OpKind)
    assert all("delete" not in op.kind and "suppr" not in op.kind for op in plan.operations)
    assert "0 · suppression" in summary_lines(plan)[-1]


def test_new_private_category_and_its_channels_are_private():
    plan = build_plan(LAYOUT, old_server())
    admin = next(op for op in plan.of(OpKind.CREATE_CATEGORY) if op.key == "administration")
    logs = next(op for op in plan.of(OpKind.CREATE_CHANNEL) if op.key == "logs")
    assert admin.private and logs.private
    reunions = next(op for op in plan.of(OpKind.CREATE_CHANNEL) if op.key == "reunions")
    assert not reunions.private
    assert any(title.startswith("Permissions posées") for title, _ in plan_sections(plan))


def test_plan_is_deterministic_and_independent_of_discord_order():
    snapshot = old_server()
    shuffled = list(snapshot.channels)
    random.Random(4).shuffle(shuffled)
    other = GuildSnapshot(tuple(reversed(snapshot.categories)), tuple(shuffled), snapshot.roles, BOT_PERMS, 5)
    first, second = build_plan(LAYOUT, snapshot), build_plan(LAYOUT, other)
    assert first.fingerprint == build_plan(LAYOUT, snapshot).fingerprint
    assert [op for op in first.operations if op.actionable] == [op for op in second.operations if op.actionable]
    assert first.fingerprint == second.fingerprint


def test_fingerprint_changes_when_the_server_changes():
    before = build_plan(LAYOUT, old_server())
    channels = tuple(GuildChannel(c.id, "annonces-old" if c.name == "annonces" else c.name, c.type, c.category_id) for c in old_server().channels)
    after = build_plan(LAYOUT, old_server(channels=channels))
    assert before.fingerprint != after.fingerprint


def test_second_run_on_a_conforming_server_changes_nothing():
    plan = build_plan(LAYOUT, old_server(), options=RedesignOptions(create_optional=True, create_roles=True))
    after, bindings, categories = apply_in_memory(plan, old_server())
    again = build_plan(LAYOUT, after, saved_bindings=bindings, saved_categories=categories, options=RedesignOptions(True, True))
    assert again.actions == []
    assert not again.can_apply
    assert keys(again, OpKind.CREATE_CHANNEL) == [] and keys(again, OpKind.CREATE_ROLE) == []


def test_without_option_existing_optional_channels_are_reused_not_created():
    snapshot = old_server()
    channels = snapshot.channels + (GuildChannel(300, "ressources", "text", 10),)
    plan = build_plan(LAYOUT, old_server(channels=channels))
    assert "ressources" not in keys(plan, OpKind.CREATE_CHANNEL)
    assert any(op.key == "ressources" for op in plan.of(OpKind.RENAME_CHANNEL))  # ancien nom : renommé
    assert "votes" in keys(plan, OpKind.SKIP)  # absent et facultatif : ignoré


def test_duplicates_are_never_resolved_at_random():
    snapshot = old_server()
    channels = snapshot.channels + (GuildChannel(301, "annonces", "text", 10),)
    plan = build_plan(LAYOUT, old_server(channels=channels))
    assert "annonces" in keys(plan, OpKind.MANUAL)
    assert "annonces" not in keys(plan, OpKind.CREATE_CHANNEL)
    assert any("2 salons s'appellent « annonces »" in risk for risk in plan.risks)
    assert analyse(old_server(channels=channels)).duplicates == (("annonces", 2),)


def test_rename_to_a_name_already_taken_is_left_to_a_human():
    snapshot = old_server()
    channels = snapshot.channels + (GuildChannel(302, "idées", "voice", 15),)  # même nom, autre type : pas de conflit
    plan = build_plan(LAYOUT, old_server(channels=channels))
    assert "idees" in keys(plan, OpKind.RENAME_CHANNEL)
    channels = snapshot.channels + (GuildChannel(303, "game-design", "text", None),)
    plan = build_plan(LAYOUT, old_server(channels=channels))
    # « game-design » existe déjà hors catégorie : il est réutilisé (nom exact prioritaire) et gameplay-et-ia reste intact.
    assert any(op.key == "gameplay" and op.object_id == 303 for op in plan.of(OpKind.MOVE_CHANNEL))
    assert "gameplay-et-ia" in plan.untouched


def test_unmanageable_channel_requires_manual_action():
    snapshot = old_server()
    channels = tuple(
        GuildChannel(c.id, c.name, c.type, c.category_id, manageable=False) if c.name == "bugs-et-tests" else c
        for c in snapshot.channels
    )
    plan = build_plan(LAYOUT, old_server(channels=channels))
    assert "bugs" in keys(plan, OpKind.MANUAL) and "bugs" not in keys(plan, OpKind.RENAME_CHANNEL)


def test_missing_bot_permission_blocks_application():
    plan = build_plan(LAYOUT, old_server(bot_permissions=frozenset()))
    assert plan.blockers and not plan.can_apply
    assert "Gérer les salons" in plan.blockers[0]
    # « Gérer les rôles » n'est exigée que si des rôles doivent être créés.
    assert not any("rôles" in b for b in build_plan(LAYOUT, old_server()).blockers)
    with_roles = build_plan(LAYOUT, old_server(), options=RedesignOptions(create_roles=True))
    assert any("Gérer les rôles" in b for b in with_roles.blockers)


def test_roles_are_only_created_on_request_and_existing_ones_untouched():
    roles = old_server().roles + (GuildRole(9, "Testeur", 1, dangerous=("administrator",)),)
    plan = build_plan(LAYOUT, old_server(roles=roles))
    assert keys(plan, OpKind.CREATE_ROLE) == []
    plan = build_plan(LAYOUT, old_server(roles=roles, bot_permissions=BOT_PERMS | {"manage_roles"}), options=RedesignOptions(create_roles=True))
    assert set(keys(plan, OpKind.CREATE_ROLE)) == {"responsable", "game_design", "developpement", "art"}
    assert any(op.key == "testeur" and op.kind is OpKind.KEEP for op in plan.operations)


def test_configuration_updates_are_planned_by_identifier():
    saved = {"bienvenue": 100, "decisions": 104, "planning": 999}  # 999 : salon supprimé depuis
    plan = build_plan(LAYOUT, old_server(), saved_bindings=saved, alerts_channel=None)
    binds = {op.key: op for op in plan.of(OpKind.BIND)}
    assert "bienvenue" not in binds and "decisions" not in binds  # déjà associés au bon identifiant
    assert binds["planning"].object_id == 102  # retrouvé par son ancien nom, associé par identifiant
    assert binds["comptes_rendus"].object_id is None  # sera associé au salon créé
    assert any(op.key == "alerts_channel" for op in plan.of(OpKind.MANUAL))
    assert not any(op.key == "alerts_channel" for op in build_plan(LAYOUT, old_server(), alerts_channel="logs").operations)


def test_category_left_empty_is_kept_and_reported():
    plan = build_plan(LAYOUT, old_server())
    manual = [op for op in plan.of(OpKind.MANUAL) if op.subject == "category"]
    assert [op.target for op in manual] == ["04 · GITLAB AUTOMATISÉ"]
    assert "jamais supprimée" in manual[0].expected


def test_private_category_visible_to_everyone_is_reported_not_modified():
    snapshot = old_server()
    categories = snapshot.categories + (GuildCategory(30, "ADMINISTRATION", 9, everyone_can_view=True),)
    plan = build_plan(LAYOUT, old_server(categories=categories))
    assert "administration" not in keys(plan, OpKind.CREATE_CATEGORY)
    assert any("ADMINISTRATION" in risk and "@everyone" in risk for risk in plan.risks)


def test_every_operation_has_an_expected_result_and_sections_fit():
    plan = build_plan(LAYOUT, old_server(), options=RedesignOptions(True, True))
    assert all(op.expected for op in plan.operations)
    titles = [title for title, _ in plan_sections(plan)]
    assert titles[0].startswith("Catégories à créer")
    assert any(t.startswith("Interventions manuelles") for t in titles)
    assert set(plan.counts) == set(OpKind) and all(kind in ACTIONABLE or kind in (OpKind.KEEP, OpKind.SKIP, OpKind.MANUAL) for kind in OpKind)


def test_building_a_plan_does_not_modify_its_inputs():
    snapshot = old_server()
    saved = {"bienvenue": 100}
    build_plan(LAYOUT, snapshot, saved_bindings=saved)
    assert snapshot == old_server() and saved == {"bienvenue": 100}


def test_rename_clash_with_a_saved_channel_is_left_to_a_human():
    channels = old_server().channels + (GuildChannel(303, "game-design", "text", 11),)
    plan = build_plan(LAYOUT, old_server(channels=channels), saved_bindings={"gameplay": 106})
    manual = next(op for op in plan.of(OpKind.MANUAL) if op.key == "gameplay")
    assert manual.object_id == 106 and "doublon" in manual.expected
    assert "gameplay" not in keys(plan, OpKind.RENAME_CHANNEL)
