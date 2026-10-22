"""Tests du serveur de webhooks : vraies requêtes HTTP sur un port local temporaire."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.database import GitLabEventRepository
from src.gitlab.dispatcher import NotificationDispatcher
from src.gitlab.rules import GitLabRules
from src.gitlab.webhook import MAX_BODY_BYTES, WEBHOOK_PATH, GitLabWebhookServer, create_webhook_app
from src.gitlab.samples import merge_request_payload, pipeline_payload, push_payload

SECRET = "secret-de-test-0123456789"
RULES = GitLabRules(routes={"push": "gitlab_activite", "merge_request": "gitlab_activite", "pipeline": "builds"})


class FakeSender:
    def __init__(self) -> None:
        self.sent = []

    async def send_notification(self, channel_id, notification):
        self.sent.append((channel_id, notification))


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def dispatcher(db, sender):
    return NotificationDispatcher(GitLabEventRepository(db), RULES, sender, lambda: {"gitlab_activite": 1, "builds": 2})


async def make_client(dispatcher, project_id=None):
    app = create_webhook_app(secret=SECRET, project_id=project_id, dispatcher=dispatcher)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
async def client(dispatcher):
    client = await make_client(dispatcher)
    yield client
    await client.close()


def headers(event="Push Hook", token=SECRET, uuid="0b1f1c9e-0000-4000-8000-000000000001", **extra):
    result = {"Content-Type": "application/json", "X-Gitlab-Event": event, **extra}
    if token is not None:
        result["X-Gitlab-Token"] = token
    if uuid is not None:
        result["X-Gitlab-Event-UUID"] = uuid
    return result


async def post(client, payload, **kwargs):
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    response = await client.post(WEBHOOK_PATH, data=body, headers=headers(**kwargs))
    return response.status, await response.json()


# --- Événements valides --------------------------------------------------------


@pytest.mark.parametrize(
    "event, payload, channel",
    [
        ("Push Hook", push_payload(), 1),
        ("Merge Request Hook", merge_request_payload(), 1),
        ("Pipeline Hook", pipeline_payload(), 2),
    ],
)
async def test_valid_events_are_queued_and_delivered(client, dispatcher, sender, event, payload, channel):
    status, body = await post(client, payload, event=event)
    assert (status, body["status"]) == (200, "queued")
    await dispatcher.drain()
    assert [c for c, _ in sender.sent] == [channel]


async def test_redelivery_is_acknowledged_but_not_notified_twice(client, dispatcher, sender):
    assert (await post(client, push_payload()))[1]["status"] == "queued"
    assert (await post(client, push_payload()))[1]["status"] == "duplicate"
    await dispatcher.drain()
    assert len(sender.sent) == 1


async def test_idempotency_key_has_priority_over_event_uuid(client):
    first = await post(client, push_payload(), uuid="uuid-a", **{"Idempotency-Key": "cle-1"})
    retry = await post(client, push_payload(), uuid="uuid-b", **{"Idempotency-Key": "cle-1"})
    assert first[1]["status"] == "queued"
    assert retry[1]["status"] == "duplicate"


async def test_without_delivery_header_the_body_hash_is_used(client):
    assert (await post(client, push_payload(), uuid=None))[1]["status"] == "queued"
    assert (await post(client, push_payload(), uuid=None))[1]["status"] == "duplicate"


async def test_unsupported_event_is_acknowledged_and_ignored(client, dispatcher, sender):
    status, body = await post(client, {"object_kind": "issue"}, event="Issue Hook")
    assert (status, body["status"]) == (200, "ignored")
    await dispatcher.drain()
    assert sender.sent == []


async def test_filtered_event_is_ignored(client):
    status, body = await post(client, pipeline_payload(status="running"), event="Pipeline Hook")
    assert (status, body["status"]) == (200, "ignored")


# --- Secrets incorrects --------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "mauvais-secret", SECRET + "x", SECRET.upper()])
async def test_wrong_or_missing_secret_is_rejected(client, dispatcher, sender, token):
    status, body = await post(client, push_payload(), token=token)
    assert (status, body["status"]) == (401, "unauthorized")
    assert SECRET not in json.dumps(body)
    await dispatcher.drain()
    assert sender.sent == []


async def test_wrong_secret_is_rejected_before_reading_body(client):
    status, _ = await post(client, b"{pas du json", token="mauvais")
    assert status == 401


async def test_wrong_secret_does_not_consume_the_delivery(client):
    await post(client, push_payload(), token="mauvais")
    assert (await post(client, push_payload()))[1]["status"] == "queued"


async def test_secret_is_never_logged(client, caplog):
    await post(client, push_payload(), token="tentative-secrete-123")
    await post(client, push_payload())
    assert "tentative-secrete-123" not in caplog.text
    assert SECRET not in caplog.text


def test_app_requires_a_secret(dispatcher):
    with pytest.raises(ValueError):
        create_webhook_app(secret="", project_id=None, dispatcher=dispatcher)


# --- Requêtes invalides --------------------------------------------------------


async def test_get_is_not_allowed(client):
    response = await client.get(WEBHOOK_PATH, headers=headers())
    assert response.status == 405


async def test_unknown_path(client):
    response = await client.post("/autre", data="{}", headers=headers())
    assert response.status == 404


async def test_wrong_content_type(client):
    response = await client.post(WEBHOOK_PATH, data="a=b", headers={**headers(), "Content-Type": "application/x-www-form-urlencoded"})
    assert response.status == 415


@pytest.mark.parametrize("body", [b"{pas du json", b"\xff\xfe", b"[1, 2]", b"null"])
async def test_malformed_json(client, body):
    status, data = await post(client, body)
    assert status == 400
    assert data["status"] == "invalid_json"


async def test_incomplete_payload_is_rejected_and_not_stored(client):
    status, data = await post(client, {"object_kind": "push"})
    assert (status, data["status"]) == (400, "invalid_payload")
    assert (await post(client, push_payload()))[0] == 200


async def test_event_header_and_body_mismatch(client):
    status, data = await post(client, pipeline_payload(), event="Push Hook")
    assert (status, data["status"]) == (400, "invalid_payload")


async def test_body_too_large(client):
    big = json.dumps({"object_kind": "push", "padding": "x" * (MAX_BODY_BYTES + 10)})
    status, data = await post(client, big)
    assert (status, data["status"]) == (413, "payload_too_large")


async def test_project_filter(dispatcher):
    client = await make_client(dispatcher, project_id=1)
    try:
        status, data = await post(client, push_payload())
        assert (status, data["status"]) == (403, "forbidden_project")
    finally:
        await client.close()
    client = await make_client(dispatcher, project_id=4242)
    try:
        assert (await post(client, push_payload()))[0] == 200
    finally:
        await client.close()


# --- Erreurs -------------------------------------------------------------------


async def test_internal_error_returns_500_without_details(dispatcher, monkeypatch):
    def boom(event):
        raise RuntimeError("détail interne secret")

    monkeypatch.setattr(dispatcher, "submit", boom)
    client = await make_client(dispatcher)
    try:
        status, data = await post(client, push_payload())
    finally:
        await client.close()
    assert status == 500
    assert "détail interne" not in json.dumps(data)


async def test_full_queue_returns_503(db, sender):
    dispatcher = NotificationDispatcher(GitLabEventRepository(db), RULES, sender, lambda: {}, queue_size=1)
    client = await make_client(dispatcher)
    try:
        assert (await post(client, push_payload(), uuid="u1"))[0] == 200
        status, data = await post(client, push_payload(after="1" * 40), uuid="u2")  # autre push
        assert (status, data["status"]) == (503, "busy")
    finally:
        await client.close()


async def test_server_start_and_stop(dispatcher, unused_tcp_port):
    app = create_webhook_app(secret=SECRET, project_id=None, dispatcher=dispatcher)
    server = GitLabWebhookServer(app, "127.0.0.1", unused_tcp_port)
    assert await server.start()
    assert server.listening
    await server.stop()
    assert not server.listening


async def test_port_already_in_use_is_reported_without_crashing(dispatcher, unused_tcp_port):
    first = GitLabWebhookServer(create_webhook_app(secret=SECRET, project_id=None, dispatcher=dispatcher), "127.0.0.1", unused_tcp_port)
    second = GitLabWebhookServer(create_webhook_app(secret=SECRET, project_id=None, dispatcher=dispatcher), "127.0.0.1", unused_tcp_port)
    assert await first.start()
    try:
        assert not await second.start()
        assert "impossible d'écouter" in second.error
        assert not second.listening
    finally:
        await first.stop()


async def test_demo_script_payloads_are_accepted(client, dispatcher, sender):
    from src.gitlab.demo import EVENTS

    for index, (event, build) in enumerate(EVENTS.values()):
        status, body = await post(client, build(), event=event, uuid=f"demo-{index}")
        assert (status, body["status"]) == (200, "queued")
    await dispatcher.drain()
    assert len(sender.sent) == len(EVENTS)


async def test_error_responses_do_not_reflect_large_or_multiline_input(client, caplog):
    status, data = await post(client, {"object_kind": "x" * 100_000 + "\nINFO faux journal"}, event="Push Hook")
    assert status == 400
    assert len(json.dumps(data)) < 300
    assert "faux journal" not in caplog.text


async def test_secret_never_appears_in_any_response_or_log(client, caplog):
    for kwargs in ({}, {"token": "x"}, {"event": "Issue Hook"}):
        for payload in (push_payload(), b"{oops", {"object_kind": "push"}):
            response = await client.post(WEBHOOK_PATH, data=payload if isinstance(payload, bytes) else json.dumps(payload), headers=headers(**kwargs))
            assert SECRET not in await response.text()
            assert SECRET not in str(response.headers)
    assert SECRET not in caplog.text
