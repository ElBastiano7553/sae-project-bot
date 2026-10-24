"""Idempotence des messages permanents, avec une fausse passerelle Discord."""

from __future__ import annotations

import asyncio

import pytest

from src.config import ConfigError
from src.database import ManagedMessageRepository
from src.services.content import MessagePayload, MessageTemplate, footer_for, footer_markers
from src.services.permanent_messages import GatewayError, MessageNotFound, PermanentMessageService, SyncAction

CHANNEL = 111
OTHER_CHANNEL = 222


class FakeGateway:
    """Simule Discord en mémoire et compte les appels."""

    def __init__(self) -> None:
        self.messages: dict[int, tuple[int, MessagePayload]] = {}  # message_id -> (salon, contenu)
        self.next_id = 1
        self.sent = 0
        self.edited = 0
        self.pinned: list[int] = []
        self.fail_pin = False
        self.fail_send = False
        self.fail_edit = False
        self.fail_delete = False
        self.vanish_on_edit = False  # le message disparaît entre la vérification et la modification
        self.deleted: list[int] = []
        self.slow = False  # rend la main à la boucle pendant l'envoi (pour tester la concurrence)

    async def message_exists(self, channel_id, message_id):
        return message_id in self.messages and self.messages[message_id][0] == channel_id

    async def find_managed_message(self, channel_id, keys):
        for message_id, (chan, payload) in self.messages.items():
            if chan == channel_id and payload.footer in footer_markers(keys):
                return message_id
        return None

    async def send(self, channel_id, payload):
        if self.fail_send:
            raise GatewayError("accès refusé")
        if self.slow:
            await asyncio.sleep(0)
        message_id = self.next_id
        self.next_id += 1
        self.messages[message_id] = (channel_id, payload)
        self.sent += 1
        return message_id

    async def edit(self, channel_id, message_id, payload):
        if self.fail_edit:
            raise GatewayError("modification refusée")
        if self.vanish_on_edit:
            del self.messages[message_id]
            raise MessageNotFound("message supprimé")
        self.messages[message_id] = (channel_id, payload)
        self.edited += 1

    async def delete(self, channel_id, message_id):
        if self.fail_delete:
            raise GatewayError("suppression refusée")
        self.messages.pop(message_id, None)
        self.deleted.append(message_id)

    async def pin(self, channel_id, message_id):
        if self.fail_pin:
            raise GatewayError("permission manquante")
        self.pinned.append(message_id)


class MutableRenderer:
    def __init__(self) -> None:
        self.text = "Version 1"

    def __call__(self, template: MessageTemplate) -> MessagePayload:
        return MessagePayload(template.title, self.text, 0, footer_for(template.key))


TEMPLATES = [
    MessageTemplate(key="bienvenue", channel_key="bienvenue", title="Bienvenue", description="-"),
    MessageTemplate(key="annonces", channel_key="annonces", title="Règles", description="-"),
]
BINDINGS = {"bienvenue": CHANNEL, "annonces": CHANNEL + 1}


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def renderer():
    return MutableRenderer()


@pytest.fixture
def repo(db):
    return ManagedMessageRepository(db)


@pytest.fixture
def service(repo, gateway, renderer):
    return PermanentMessageService(repo, gateway, TEMPLATES, renderer)


def actions(results):
    return {r.key: r.action for r in results}


async def test_first_sync_creates_and_pins(service, gateway, repo):
    results = await service.sync(BINDINGS)
    assert actions(results) == {"bienvenue": SyncAction.CREATE, "annonces": SyncAction.CREATE}
    assert gateway.sent == 2
    assert len(gateway.pinned) == 2
    assert repo.get("bienvenue").channel_id == CHANNEL


async def test_second_sync_creates_no_duplicates(service, gateway):
    await service.sync(BINDINGS)
    results = await service.sync(BINDINGS)
    assert set(actions(results).values()) == {SyncAction.UNCHANGED}
    assert gateway.sent == 2
    assert gateway.edited == 0


async def test_changed_content_edits_existing_message(service, gateway, renderer, repo):
    await service.sync(BINDINGS)
    first_id = repo.get("bienvenue").message_id
    renderer.text = "Version 2"
    results = await service.sync(BINDINGS)
    assert set(actions(results).values()) == {SyncAction.UPDATE}
    assert gateway.sent == 2
    assert repo.get("bienvenue").message_id == first_id
    assert gateway.messages[first_id][1].description == "Version 2"


async def test_deleted_message_is_recreated(service, gateway, repo):
    await service.sync(BINDINGS)
    deleted_id = repo.get("bienvenue").message_id
    del gateway.messages[deleted_id]
    results = await service.sync(BINDINGS)
    assert actions(results)["bienvenue"] is SyncAction.RECREATE
    assert repo.get("bienvenue").message_id != deleted_id
    assert gateway.sent == 3


async def test_lost_database_adopts_existing_message(service, gateway, repo):
    await service.sync(BINDINGS)
    repo.delete("bienvenue")
    results = await service.sync(BINDINGS)
    assert actions(results)["bienvenue"] is SyncAction.ADOPT
    assert gateway.sent == 2  # aucun doublon
    assert repo.get("bienvenue") is not None


async def test_channel_change_moves_message_without_leaving_a_duplicate(service, gateway, repo):
    # Lot 1 : à la demande de l'équipe, l'ancien message du bot n'est plus laissé dans l'ancien salon.
    await service.sync(BINDINGS)
    old_id = repo.get("bienvenue").message_id
    results = await service.sync({**BINDINGS, "bienvenue": OTHER_CHANNEL})
    assert actions(results)["bienvenue"] is SyncAction.MOVE
    assert old_id not in gateway.messages
    assert gateway.deleted == [old_id]
    assert repo.get("bienvenue").channel_id == OTHER_CHANNEL
    assert "ancien message supprimé" in results[0].detail


async def test_move_preview_announces_the_deletion_without_doing_it(service, gateway, repo):
    await service.sync(BINDINGS)
    results = await service.plan({**BINDINGS, "bienvenue": OTHER_CHANNEL})
    assert actions(results)["bienvenue"] is SyncAction.MOVE
    assert "sera supprimé" in results[0].detail
    assert gateway.deleted == []
    assert repo.get("bienvenue").channel_id == CHANNEL


async def test_move_keeps_new_message_when_old_one_cannot_be_deleted(service, gateway, repo):
    await service.sync(BINDINGS)
    gateway.fail_delete = True
    results = await service.sync({**BINDINGS, "bienvenue": OTHER_CHANNEL})
    assert actions(results)["bienvenue"] is SyncAction.MOVE
    assert "supprimez-le à la main" in results[0].detail
    assert repo.get("bienvenue").channel_id == OTHER_CHANNEL
    # Une nouvelle actualisation ne republie rien : le nouveau message est bien enregistré.
    await service.sync({**BINDINGS, "bienvenue": OTHER_CHANNEL})
    assert gateway.sent == 3


async def test_concurrent_refreshes_do_not_create_duplicates(repo, gateway, renderer):
    gateway.slow = True
    lock = asyncio.Lock()
    first = PermanentMessageService(repo, gateway, TEMPLATES, renderer, lock=lock)
    second = PermanentMessageService(repo, gateway, TEMPLATES, renderer, lock=lock)
    results = await asyncio.gather(first.sync(BINDINGS), second.sync(BINDINGS))
    assert gateway.sent == 2  # un seul message par clé
    assert set(actions(results[1]).values()) == {SyncAction.UNCHANGED}


async def test_failed_edit_keeps_database_consistent(service, gateway, renderer, repo):
    await service.sync(BINDINGS)
    before = repo.get("bienvenue")
    renderer.text = "Version 2"
    gateway.fail_edit = True
    results = await service.sync(BINDINGS)
    assert actions(results)["bienvenue"] is SyncAction.ERROR
    after = repo.get("bienvenue")
    assert (after.message_id, after.content_hash) == (before.message_id, before.content_hash)
    assert after.last_error == "modification refusée"
    gateway.fail_edit = False
    results = await service.sync(BINDINGS)  # la mise à jour est retentée, sans doublon
    assert actions(results)["bienvenue"] is SyncAction.UPDATE
    assert repo.get("bienvenue").last_error is None
    assert gateway.sent == 2


async def test_message_deleted_during_edit_is_recreated(service, gateway, renderer, repo):
    await service.sync(BINDINGS)
    renderer.text = "Version 2"
    gateway.vanish_on_edit = True
    results = await service.sync(BINDINGS, keys=["bienvenue"])
    assert actions(results) == {"bienvenue": SyncAction.RECREATE}
    assert gateway.sent == 3


async def test_sync_selected_keys_only(service, gateway):
    results = await service.sync(BINDINGS, keys=["annonces"])
    assert actions(results) == {"annonces": SyncAction.CREATE}
    assert gateway.sent == 1
    with pytest.raises(ConfigError):
        await service.sync(BINDINGS, keys=["inconnu"])


async def test_rebuild_publishes_new_message_then_deletes_old_one(service, gateway, repo):
    await service.sync(BINDINGS)
    old_id = repo.get("bienvenue").message_id
    preview = await service.rebuild("bienvenue", BINDINGS, apply=False)
    assert preview.action is SyncAction.REBUILD and gateway.deleted == []
    result = await service.rebuild("bienvenue", BINDINGS)
    assert result.action is SyncAction.REBUILD
    new_id = repo.get("bienvenue").message_id
    assert new_id != old_id and new_id in gateway.messages
    assert gateway.deleted == [old_id]


async def test_rebuild_failure_keeps_old_message(service, gateway, repo):
    await service.sync(BINDINGS)
    old_id = repo.get("bienvenue").message_id
    gateway.fail_send = True
    result = await service.rebuild("bienvenue", BINDINGS)
    assert result.action is SyncAction.ERROR
    assert repo.get("bienvenue").message_id == old_id
    assert old_id in gateway.messages and gateway.deleted == []


async def test_restart_finds_existing_messages(db, gateway, renderer):
    first = PermanentMessageService(ManagedMessageRepository(db), gateway, TEMPLATES, renderer)
    await first.sync(BINDINGS)
    restarted = PermanentMessageService(ManagedMessageRepository(db), gateway, TEMPLATES, renderer)
    results = await restarted.sync(BINDINGS)
    assert set(actions(results).values()) == {SyncAction.UNCHANGED}
    assert gateway.sent == 2


async def test_missing_channel_is_skipped(service, gateway):
    results = await service.sync({"annonces": CHANNEL + 1})
    assert actions(results)["bienvenue"] is SyncAction.SKIP
    assert gateway.sent == 1


async def test_plan_does_not_modify_anything(service, gateway, repo):
    results = await service.plan(BINDINGS)
    assert set(actions(results).values()) == {SyncAction.CREATE}
    assert gateway.sent == 0
    assert repo.list_all() == []


async def test_plan_after_sync_shows_unchanged(service, gateway):
    await service.sync(BINDINGS)
    results = await service.plan(BINDINGS)
    assert set(actions(results).values()) == {SyncAction.UNCHANGED}


async def test_discord_error_is_reported_and_does_not_stop_other_messages(service, gateway):
    gateway.fail_send = True
    results = await service.sync(BINDINGS)
    assert set(actions(results).values()) == {SyncAction.ERROR}
    assert "accès refusé" in results[0].detail


async def test_pin_failure_is_not_fatal_and_no_duplicate_later(service, gateway):
    gateway.fail_pin = True
    results = await service.sync(BINDINGS)
    assert actions(results)["bienvenue"] is SyncAction.CREATE
    assert "épinglage impossible" in results[0].detail
    await service.sync(BINDINGS)
    assert gateway.sent == 2


async def test_pinning_can_be_disabled(repo, gateway, renderer):
    service = PermanentMessageService(repo, gateway, TEMPLATES, renderer, pin_messages=False)
    await service.sync(BINDINGS)
    assert gateway.pinned == []


async def test_find_deleted_managed_message(service, repo):
    await service.sync(BINDINGS)
    message_id = repo.get("annonces").message_id
    assert service.find_by_message_id(message_id).key == "annonces"
    assert service.find_by_message_id(999_999) is None


# --- Règlement : ancienne clé « annonces », salon facultatif avec repli --------------------------------

RULES_CHANNEL = 333
RULES = MessageTemplate(key="reglement", channel_key="regles", title="Règles", description="-", previous_keys=("annonces",))


async def test_renamed_template_takes_over_the_published_message_without_duplicate(repo, gateway, renderer):
    """Mise à jour d'un serveur existant : le message des règles publié sous « annonces » est repris."""
    old = PermanentMessageService(repo, gateway, TEMPLATES, renderer)
    await old.sync(BINDINGS)
    published = repo.get("annonces").message_id
    new = PermanentMessageService(repo, gateway, [TEMPLATES[0], RULES], renderer, fallbacks={"regles": "annonces"})
    plan = await new.plan(BINDINGS)
    # Le pied de page change (nouvelle clé) : simple modification sur place, jamais un nouveau message.
    assert actions(plan)["reglement"] is SyncAction.UPDATE and repo.get("annonces") is not None  # l'aperçu ne modifie rien
    results = await new.sync(BINDINGS)  # #règlement n'existe pas : repli sur #annonces, même message
    assert actions(results)["reglement"] is SyncAction.UPDATE
    assert repo.get("annonces") is None and repo.get("reglement").message_id == published
    assert gateway.sent == 2

    # #règlement est créé puis associé : le message y est déplacé, l'ancien est supprimé.
    moved = await new.sync({**BINDINGS, "regles": RULES_CHANNEL})
    assert actions(moved)["reglement"] is SyncAction.MOVE
    assert repo.get("reglement").channel_id == RULES_CHANNEL
    assert published in gateway.deleted
    assert [chan for chan, _ in gateway.messages.values()].count(BINDINGS["annonces"]) == 0


async def test_fallback_is_used_only_while_the_channel_is_missing(repo, gateway, renderer):
    service = PermanentMessageService(repo, gateway, [RULES], renderer, fallbacks={"regles": "annonces"})
    assert service.channel_for(RULES, {"annonces": 1}) == 1
    assert service.channel_for(RULES, {"annonces": 1, "regles": 2}) == 2
    assert service.channel_for(RULES, {}) is None
    statuses = service.status({"annonces": 1})
    assert statuses[0].channel_id == 1


async def test_lost_database_adopts_message_with_old_key_and_old_footer(repo, gateway, renderer):
    """Base perdue : un message publié avant le renommage (« SAE Project Bot », clé « annonces ») est repris."""
    legacy = MessagePayload("Règles", "ancien", 0, "Message géré par SAE Project Bot · réf. annonces")
    gateway.messages[50] = (BINDINGS["annonces"], legacy)
    service = PermanentMessageService(repo, gateway, [RULES], renderer, fallbacks={"regles": "annonces"})
    results = await service.sync(BINDINGS)
    assert actions(results)["reglement"] is SyncAction.ADOPT
    assert repo.get("reglement").message_id == 50 and gateway.sent == 0
    assert gateway.messages[50][1].footer == footer_for("reglement")


def test_footer_uses_the_bot_name():
    assert footer_for("bienvenue") == "Message géré par Samae · réf. bienvenue"
    assert footer_markers(["x"]) == {"Message géré par Samae · réf. x", "Message géré par SAE Project Bot · réf. x"}
