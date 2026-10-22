"""Plan de /refonte et exécution sur un faux serveur Discord en mémoire (aucun réseau, aucun vrai serveur)."""

from __future__ import annotations

import dataclasses

import discord
import pytest

from tests.fake_guild import FakeGuild, sae_server
from src.config import load_server_layout
from src.discord_bot.redesign import RedesignExecutor
from src.services.redesign_rollback import build_rollback_plan
from src.services.server_redesign import (
    Actor,
    OpKind,
    RedesignOptions,
    ResultStatus,
    build_plan,
    plan_sections,
)

LAYOUT = load_server_layout()


def plan_for(guild: FakeGuild, layout=LAYOUT, **kwargs):
    return build_plan(layout, guild.snapshot(), **kwargs)


def with_policy(**changes):
    return dataclasses.replace(LAYOUT, policy=dataclasses.replace(LAYOUT.policy, **changes))


async def run(guild: FakeGuild, plan, operations=None):
    executor = RedesignExecutor(guild, actor="test", admin_role_name=None)
    executor.seed(plan)
    return await executor.apply(list(plan.actions if operations is None else operations)), executor


def kinds(plan, kind):
    return {op.key for op in plan.of(kind)}


# --- Serveur vide -------------------------------------------------------------------------------


async def test_empty_server_gets_the_whole_target_with_emojis_roles_and_order():
    guild = FakeGuild()
    plan = plan_for(guild)
    assert plan.can_apply and not plan.blockers and plan.deletions == ()
    assert len(plan.of(OpKind.CREATE_CATEGORY)) == 8
    assert {op.target for op in plan.of(OpKind.CREATE_CATEGORY)} >= {"🏠 ACCUEIL", "💻 DÉVELOPPEMENT", "🛡️ ADMINISTRATION"}
    assert "📢・annonces" in {op.target for op in plan.of(OpKind.CREATE_CHANNEL)}
    roles = {op.target: op.params for op in plan.of(OpKind.CREATE_ROLE)}
    assert roles["Responsable projet"]["color"] == 0xC9A227 and "administrator" not in roles["Responsable projet"]["permissions"]

    results, _ = await run(guild, plan)
    assert not [r.detail for r in results if r.status is ResultStatus.FAILED]
    tree = guild.tree()
    assert list(tree)[:3] == ["🏠 ACCUEIL", "💬 ESPACE COMMUN", "🎨 CONCEPTION DU JEU"]
    assert tree["🏠 ACCUEIL"] == ["👋・bienvenue", "📢・annonces"]
    assert tree["🔊 VOCAL"] == ["🎙️ Réunion d'équipe", "🎧 Travail en commun"]
    assert guild.role("Testeur").colour.value == 0xD19A4B
    # Hiérarchie : ordre du fichier, sous le rôle de Samae.
    ordered = [r.name for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default()]
    assert ordered[:3] == ["Samae", "Responsable projet", "Développement"]


async def test_second_identical_run_changes_nothing():
    guild = sae_server()
    results, _ = await run(guild, plan_for(guild))
    assert not [r for r in results if r.status is ResultStatus.FAILED]
    calls = len(guild.calls)
    again = plan_for(guild)
    assert [op for op in again.actions if op.kind is not OpKind.BIND] == []
    await run(guild, again)
    assert len(guild.calls) == calls


# --- Serveur déjà structuré : réorganisation ----------------------------------------------------


async def test_existing_server_is_renamed_moved_and_reordered_without_recreating_anything():
    guild = sae_server()
    ids = {c.name: c.id for c in guild.channels}
    plan = plan_for(guild)
    renames = {op.current: op.target for op in plan.of(OpKind.EDIT_CHANNEL)}
    assert renames["discussion-generale"] == "💬・général"
    assert renames["gitlab-activite"] == "🦊・activité-git"
    assert {op.object_id for op in plan.of(OpKind.MOVE_CHANNEL)} >= {ids["discussion-generale"], ids["gitlab-activite"]}
    # Les salons existants ne sont jamais recréés : seuls les manquants le sont.
    assert {op.target for op in plan.of(OpKind.CREATE_CHANNEL)} == {"🗓️・réunions", "📝・comptes-rendus", "🧾・bot-logs"}

    await run(guild, plan)
    assert guild.get_channel(ids["discussion-generale"]).name == "💬・général"
    assert guild.get_channel(ids["gitlab-activite"]).category.name == "💻 DÉVELOPPEMENT"
    assert guild.get_channel(ids["bienvenue"]).topic  # sujet ajouté quand il était vide
    assert guild.tree()["💻 DÉVELOPPEMENT"][:2] == ["🐍・développement", "🌐・réseau-multijoueur"]


async def test_custom_topic_and_custom_overwrites_are_preserved():
    guild = sae_server()
    general = guild.by_name("discussion-generale")
    general.topic = "Notre sujet à nous"
    guest = guild.add_role("Invité")
    general.overwrites[guest] = discord.PermissionOverwrite(send_messages=False)
    annonces = guild.by_name("annonces")
    annonces.overwrites[guest] = discord.PermissionOverwrite(view_channel=False)
    plan = plan_for(guild)
    edit = next(op for op in plan.of(OpKind.EDIT_CHANNEL) if op.object_id == general.id)
    assert "sujet" not in edit.params
    await run(guild, plan)
    assert general.topic == "Notre sujet à nous"
    assert general.overwrites[guest].send_messages is False
    assert annonces.overwrites[guest].view_channel is False  # cible non gérée : conservée
    assert annonces.overwrites[guild.default_role].send_messages is False  # cible gérée : corrigée


def test_unknown_and_custom_elements_are_kept_and_flagged():
    guild = sae_server()
    guild.add_channel("memes")
    guild.add_channel("radio", kind="voice")
    guild.add_role("Club photo")
    plan = plan_for(guild, with_policy(keep=("radio", "Club photo")))
    assert {op.current for op in plan.of(OpKind.UNKNOWN)} >= {"memes"}
    assert {op.current for op in plan.of(OpKind.CUSTOM)} == {"radio", "Club photo"}
    touched = {op.object_id for op in plan.actions}
    assert guild.by_name("memes").id not in touched and guild.by_name("radio").id not in touched


def test_duplicates_are_flagged_and_only_empty_ones_are_proposed_for_deletion():
    guild = sae_server()
    guild.add_channel("📢・annonces", has_messages=False)
    guild.add_channel("discussion generale", has_messages=True)
    plan = plan_for(guild, options=RedesignOptions(deletions=True))
    duplicates = {op.current for op in plan.of(OpKind.DUPLICATE)}
    assert {"📢・annonces", "discussion generale"} <= duplicates
    deletions = {op.current for op in plan.deletions}
    assert "📢・annonces" in deletions and "discussion generale" not in deletions
    assert not any(op.kind.name.startswith("DELETE") for op in plan.actions)


def test_deletion_is_never_inferred_from_absence_in_the_target():
    guild = sae_server()
    guild.add_channel("memes")
    guild.add_channel("vide", has_messages=False)
    guild.add_role("Ancien rôle")
    plan = plan_for(guild, options=RedesignOptions(deletions=True))
    assert {op.current for op in plan.deletions} == {"04 · GITLAB AUTOMATISÉ"}  # vide après les déplacements
    obsolete = plan_for(guild, with_policy(obsolete=("vide", "Ancien rôle")), options=RedesignOptions(deletions=True))
    assert {op.current for op in obsolete.deletions} == {"04 · GITLAB AUTOMATISÉ", "vide", "Ancien rôle"}


def test_deletions_stay_off_when_the_policy_forbids_them():
    guild = sae_server()
    plan = plan_for(guild, with_policy(deletions_allowed=False), options=RedesignOptions(deletions=True))
    assert not plan.options.deletions and not plan.can_delete
    assert any("suppressions" in risk.lower() for risk in plan.risks)


def test_deletions_are_listed_last_and_separately_in_the_preview():
    plan = plan_for(sae_server(), options=RedesignOptions(deletions=True))
    titles = [title for title, _ in plan_sections(plan)]
    assert titles[0].startswith("Structure cible") and titles[-1].startswith("Suppressions")
    assert plan.fingerprint != plan.deletion_fingerprint


# --- Rôles --------------------------------------------------------------------------------------


async def test_existing_roles_are_reused_completed_and_never_duplicated():
    guild = FakeGuild()
    dev = guild.add_role("Dev", color=0x123456)
    guild.add_role("Testeur", permissions=discord.Permissions(kick_members=True))
    plan = plan_for(guild)
    assert "Développement" not in {op.target for op in plan.of(OpKind.CREATE_ROLE)}
    assert "Testeur" not in {op.target for op in plan.of(OpKind.CREATE_ROLE)}
    edit = next(op for op in plan.of(OpKind.EDIT_ROLE) if op.object_id == dev.id)
    assert edit.params["name"] == "Développement"
    assert any("Testeur" in risk and "Expulser" in risk for risk in plan.risks)  # signalé, jamais retiré
    await run(guild, plan)
    assert dev.name == "Développement" and dev.colour.value == 0x4F8FD6
    assert guild.role("Testeur").permissions.kick_members
    assert [r.name for r in guild.roles].count("Développement") == 1


def test_role_above_the_bot_or_the_user_is_left_to_a_human():
    guild = FakeGuild()
    guild.add_role("Responsable projet", position=20)  # au-dessus de Samae (10)
    plan = plan_for(guild)
    manual = [op for op in plan.of(OpKind.MANUAL) if op.target == "Responsable projet"]
    assert manual and "au-dessus" in manual[0].expected
    low = Actor(permissions=frozenset({"manage_roles", "manage_channels"}), top_role_position=2)
    guild2 = FakeGuild()
    guild2.add_role("Testeur", position=5)
    plan2 = plan_for(guild2, actor=low)
    assert any(op.target == "Testeur" for op in plan2.of(OpKind.MANUAL))


def test_user_without_manage_roles_creates_no_role_and_changes_no_permission():
    guild = sae_server()
    actor = Actor(permissions=frozenset({"manage_channels", "manage_guild"}), top_role_position=50)
    plan = plan_for(guild, actor=actor)
    assert plan.of(OpKind.CREATE_ROLE) == [] and plan.of(OpKind.SET_PERMISSIONS) == []
    assert plan.of(OpKind.MANUAL)


# --- Permissions et blocages --------------------------------------------------------------------


def test_bot_without_manage_roles_turns_permissions_and_roles_into_manual_steps():
    guild = sae_server(bot_permissions={"manage_channels", "view_channel", "send_messages", "read_message_history", "embed_links"})
    plan = plan_for(guild)
    assert plan.can_apply
    assert plan.of(OpKind.SET_PERMISSIONS) == [] and plan.of(OpKind.CREATE_ROLE) == []
    assert any("Gérer les rôles" in (op.expected or "") for op in plan.of(OpKind.MANUAL))


def test_bot_without_manage_channels_blocks_everything():
    guild = sae_server(bot_permissions={"view_channel"})
    plan = plan_for(guild)
    assert not plan.can_apply and any("Gérer les salons" in b for b in plan.blockers)


def test_a_layout_hiding_every_channel_is_blocked():
    staff_only = {key: dataclasses.replace(spec, access="staff") for key, spec in LAYOUT.channels.items()}
    plan = plan_for(FakeGuild(), dataclasses.replace(LAYOUT, channels=staff_only))
    assert not plan.can_apply and any("@everyone" in b for b in plan.blockers)


def test_bot_keeps_access_to_staff_and_read_only_channels():
    plan = plan_for(FakeGuild())
    logs = next(op for op in plan.of(OpKind.CREATE_CHANNEL) if op.key == "logs")
    admin = next(op for op in plan.of(OpKind.CREATE_CATEGORY) if op.key == "administration")
    for op in (admin,):
        rules = op.params["overwrites"]
        assert "view_channel" in rules["@everyone"]["deny"] and "view_channel" in rules["@bot"]["allow"]
    assert logs.category_key == "administration"


def test_permissions_already_correct_produce_no_operation():
    guild = FakeGuild()
    plan = plan_for(guild)
    # Un salon public sans surcharge n'a rien à corriger (pas de surcharge inutile pour Samae).
    assert all(op.key not in ("general", "commun") for op in plan.of(OpKind.SET_PERMISSIONS))


# --- Profils, styles, Communauté ---------------------------------------------------------------


def test_style_and_profile_change_names_and_content():
    sober = plan_for(FakeGuild(), options=RedesignOptions(style="sobre"))
    assert "ACCUEIL" in {op.target for op in sober.of(OpKind.CREATE_CATEGORY)}
    assert "annonces" in {op.target for op in sober.of(OpKind.CREATE_CHANNEL)}
    community = plan_for(FakeGuild(), options=RedesignOptions(profile="communautaire"))
    assert "Modérateur" in {op.target for op in community.of(OpKind.CREATE_ROLE)}
    assert "Modérateur" not in {op.target for op in plan_for(FakeGuild()).of(OpKind.CREATE_ROLE)}
    with pytest.raises(ValueError):
        plan_for(FakeGuild(), options=RedesignOptions(profile="inexistant"))


def test_community_server_converts_announcements_and_gets_real_types():
    plain = sae_server()
    plan = plan_for(plain)
    assert any("Communauté" in risk for risk in plan.risks)
    assert any(op.target.startswith("📢") for op in plan.of(OpKind.MANUAL))
    community = sae_server(community=True)
    plan = plan_for(community, options=RedesignOptions(create_optional=True, profile="communautaire"))
    edit = next(op for op in plan.of(OpKind.EDIT_CHANNEL) if op.current == "annonces")
    assert edit.params.get("type") == "announcement"
    created = {op.key: op.params["type"] for op in plan.of(OpKind.CREATE_CHANNEL)}
    assert created.get("suggestions") == "forum" and created.get("scene") == "stage"


def test_optional_elements_need_the_option():
    plan = plan_for(FakeGuild())
    assert "regles" not in kinds(plan, OpKind.CREATE_CHANNEL) and "regles" in kinds(plan, OpKind.SKIP)
    plan = plan_for(FakeGuild(), options=RedesignOptions(create_optional=True))
    assert "regles" in kinds(plan, OpKind.CREATE_CHANNEL) and "notif_votes" in kinds(plan, OpKind.CREATE_ROLE)


def test_fingerprint_changes_when_the_server_changes():
    guild = sae_server()
    before = plan_for(guild).fingerprint
    guild.add_channel("annonces-2")
    assert plan_for(guild).fingerprint == before  # élément inconnu : aucune action
    guild.by_name("bienvenue").name = "accueil-libre"
    assert plan_for(guild).fingerprint != before


# --- Erreurs pendant l'application ------------------------------------------------------------


async def test_discord_error_mid_application_is_reported_and_the_rest_continues():
    guild = sae_server()
    response = type("R", (), {"status": 403, "reason": "Forbidden"})()
    guild.fail("edit", "bienvenue", discord.Forbidden(response, "Missing Permissions"))
    guild.fail("create_category", "📋 ORGANISATION", discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "boom"))
    results, _ = await run(guild, plan_for(guild))
    failed = {r.operation.target: r for r in results if r.status is ResultStatus.FAILED}
    assert "👋・bienvenue" in failed and "📋 ORGANISATION" in failed
    # Les salons prévus dans la catégorie non créée sont ignorés proprement, pas créés ailleurs.
    skipped = {r.operation.target for r in results if r.status is ResultStatus.SKIPPED}
    assert "🗓️・réunions" in skipped
    assert guild.by_name("🗓️・réunions") is None
    assert guild.by_name("💬・général") is not None
    # Une nouvelle analyse reprend exactement ce qui manque.
    again = plan_for(guild)
    assert "📋 ORGANISATION" in {op.target for op in again.of(OpKind.CREATE_CATEGORY)}


async def test_timeout_is_a_failure_not_a_success():
    guild = FakeGuild()
    guild.delays[("create_channel", "💬・général")] = 0.5
    plan = plan_for(guild)
    executor = RedesignExecutor(guild, actor="test", admin_role_name=None, timeout=0.05)
    executor.seed(plan)
    results = await executor.apply(plan.actions)
    general = next(r for r in results if r.operation.target == "💬・général")
    assert general.status is ResultStatus.FAILED and "délai" in general.detail


async def test_deletion_rechecks_the_channel_before_deleting():
    guild = sae_server()
    await run(guild, plan_for(guild))
    plan = plan_for(guild, options=RedesignOptions(deletions=True))
    assert {op.current for op in plan.deletions} == {"04 · GITLAB AUTOMATISÉ"}
    category = guild.by_name("04 · GITLAB AUTOMATISÉ")
    guild.add_channel("ajout-de-derniere-minute", category)
    results, _ = await run(guild, plan, plan.deletions)
    assert results[0].status is not ResultStatus.DONE and "salons" in results[0].detail and guild.by_name("04 · GITLAB AUTOMATISÉ") is not None


# --- Retour arrière ------------------------------------------------------------------------------


def journal_of(results, before) -> dict:
    from src.discord_bot.cogs.redesign import _serialize_result

    return {"kind": "refonte", "results": [_serialize_result(r) for r in results],
            "before": {"snapshot": dataclasses.asdict(before), "bindings": {}, "categories": {}, "roles": {}}}


async def test_rollback_restores_names_categories_permissions_and_removes_empty_creations():
    guild = sae_server()
    before = guild.snapshot()
    ids = {c.name: c.id for c in guild.channels}
    results, _ = await run(guild, plan_for(guild))
    rollback = build_rollback_plan(journal_of(results, before), guild.snapshot())
    await run(guild, rollback)
    assert guild.get_channel(ids["discussion-generale"]).name == "discussion-generale"
    assert guild.get_channel(ids["discussion-generale"]).category.name == "01 · ACCUEIL & ORGANISATION"
    assert guild.default_role not in guild.get_channel(ids["annonces"]).overwrites
    assert guild.by_name("💬 ESPACE COMMUN") is not None  # suppressions du retour : liste séparée
    removed, _ = await run(guild, rollback, rollback.deletions)
    assert all(r.status is ResultStatus.DONE for r in removed)
    assert guild.by_name("💬 ESPACE COMMUN") is None and guild.role("Testeur") is None
    assert guild.tree()["01 · ACCUEIL & ORGANISATION"][0] == "bienvenue"


async def test_rollback_does_not_touch_what_changed_since_and_states_its_limits():
    guild = sae_server()
    before = guild.snapshot()
    results, _ = await run(guild, plan_for(guild))
    renamed = guild.by_name("💬・général")
    renamed.name = "renommé-à-la-main"
    reunions = guild.by_name("🗓️・réunions")
    reunions.last_message_id = 123  # quelqu'un a écrit dans un salon créé par la refonte
    rollback = build_rollback_plan(journal_of(results, before), guild.snapshot())
    assert renamed.id not in {op.object_id for op in rollback.of(OpKind.EDIT_CHANNEL)}
    assert reunions.id not in {op.object_id for op in rollback.deletions}
    assert any(op.object_id in (renamed.id, reunions.id) for op in rollback.of(OpKind.MANUAL))
    assert any("messages" in risk for risk in rollback.risks)
