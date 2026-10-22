"""Tests du serveur de webhooks GitHub : vraies requêtes HTTP sur un port local temporaire."""

import json
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.database.github import GitHubDeliveryRepository
from src.github.dispatcher import GitHubDispatcher
from src.github.samples import issue_payload, ping_payload, pull_request_payload, push_payload, workflow_run_payload
from src.github.security import compute_signature, signature_matches
from src.github.webhook import MAX_BODY_BYTES, WEBHOOK_PATH, GitHubWebhookServer, create_github_app

SECRET = "secret-github-de-test-0123456789"
REPOSITORY = "ElBastiano7553/game-project"


class FakeSender:
    def __init__(self) -> None:
        self.sent = []

    async def send_notification(self, channel_id, notification):
        self.sent.append((channel_id, notification))


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def repo(db):
    return GitHubDeliveryRepository(db)


@pytest.fixture
def dispatcher(repo, sender):
    return GitHubDispatcher(repo, sender, lambda: {"gitlab_activite": 1})


@pytest.fixture
async def client(dispatcher):
    app = create_github_app(secret=SECRET, repository=REPOSITORY, dispatcher=dispatcher)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


def encode(payload) -> bytes:
    if isinstance(payload, bytes):
        return payload
    return (payload if isinstance(payload, str) else json.dumps(payload)).encode("utf-8")


async def post(client, payload, *, event="push", delivery=None, secret=SECRET, signature=None, content_type="application/json", extra=None):
    body = encode(payload)
    headers = {"Content-Type": content_type, "X-GitHub-Event": event}
    if delivery is not False:
        headers["X-GitHub-Delivery"] = delivery or str(uuid.uuid4())
    if signature is None:
        signature = compute_signature(secret, body)
    if signature is not False:
        headers["X-Hub-Signature-256"] = signature
    headers.update(extra or {})
    response = await client.post(WEBHOOK_PATH, data=body, headers=headers)
    return response.status, await response.json()


# --- Signature (fonction pure) ----------------------------------------------------


def test_signature_matches_github_reference_example():
    # Exemple officiel de la documentation GitHub (validating-webhook-deliveries).
    body = b"Hello, World!"
    signature = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert signature_matches("It's a Secret to Everybody", body, signature)
    assert compute_signature("It's a Secret to Everybody", body) == signature


@pytest.mark.parametrize(
    "received",
    [
        None,
        "",
        "sha256=",
        "sha1=" + "0" * 40,
        "sha256=" + "0" * 64,
        "sha256=" + "z" * 64,
        "sha256=" + "0" * 63,
        compute_signature("autre-secret-0123456789", b"{}"),
    ],
)
def test_invalid_signatures_are_rejected(received):
    assert not signature_matches(SECRET, b"{}", received)


def test_empty_secret_never_matches():
    assert not signature_matches("", b"{}", compute_signature("", b"{}"))


def test_signature_covers_the_exact_body():
    signature = compute_signature(SECRET, b'{"a": 1}')
    assert signature_matches(SECRET, b'{"a": 1}', signature)
    assert not signature_matches(SECRET, b'{"a":1}', signature)


def test_signature_is_compared_with_compare_digest(monkeypatch):
    calls = []
    import hmac

    real = hmac.compare_digest
    monkeypatch.setattr("src.github.security.hmac.compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert signature_matches(SECRET, b"{}", compute_signature(SECRET, b"{}"))
    assert calls == [1]


# --- Événements valides --------------------------------------------------------


@pytest.mark.parametrize(
    "event, build",
    [
        ("push", push_payload),
        ("pull_request", pull_request_payload),
        ("issues", issue_payload),
        ("workflow_run", workflow_run_payload),
    ],
)
async def test_supported_events_are_queued_and_delivered_to_gitlab_activite(client, dispatcher, sender, event, build):
    status, body = await post(client, build(), event=event)
    assert (status, body["status"]) == (200, "queued")
    await dispatcher.drain()
    assert [channel for channel, _ in sender.sent] == [1]
    assert sender.sent[0][1].footer == f"GitHub · {REPOSITORY}"


async def test_ping_is_answered_without_notification(client, dispatcher, sender, repo):
    status, body = await post(client, ping_payload(), event="ping")
    assert (status, body["status"]) == (200, "pong")
    await dispatcher.drain()
    assert sender.sent == []
    assert repo.last_received_at() is None


async def test_unsupported_event_is_acknowledged_and_ignored(client, dispatcher, sender):
    status, body = await post(client, {"repository": {"full_name": REPOSITORY}}, event="star")
    assert (status, body["status"]) == (200, "ignored")
    await dispatcher.drain()
    assert sender.sent == []


async def test_filtered_action_is_ignored(client):
    payload = pull_request_payload()
    payload["action"] = "synchronize"
    status, body = await post(client, payload, event="pull_request")
    assert (status, body["status"]) == (200, "ignored")


async def test_successful_workflow_is_ignored(client):
    payload = workflow_run_payload()
    payload["workflow_run"]["conclusion"] = "success"
    status, body = await post(client, payload, event="workflow_run")
    assert (status, body["status"]) == (200, "ignored")


# --- Doublons -------------------------------------------------------------------


async def test_same_delivery_id_is_notified_once(client, dispatcher, sender):
    delivery = str(uuid.uuid4())
    first = await post(client, push_payload(), delivery=delivery)
    second = await post(client, push_payload(), delivery=delivery)
    assert first == (200, {"status": "queued"})
    assert second == (200, {"status": "duplicate"})
    await dispatcher.drain()
    assert len(sender.sent) == 1


async def test_same_fact_with_new_delivery_id_is_notified_once(client, dispatcher, sender):
    assert (await post(client, push_payload()))[1]["status"] == "queued"
    assert (await post(client, push_payload()))[1]["status"] == "duplicate"
    await dispatcher.drain()
    assert len(sender.sent) == 1


async def test_different_events_with_same_delivery_id_are_distinct(client, dispatcher, sender):
    # La clé de livraison inclut l'événement : pas de collision entre deux types différents.
    delivery = str(uuid.uuid4())
    assert (await post(client, push_payload(), delivery=delivery))[1]["status"] == "queued"
    assert (await post(client, issue_payload(), event="issues", delivery=delivery))[1]["status"] == "queued"


# --- Sécurité ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "signature",
    [False, "", "sha256=" + "0" * 64, "sha1=abc", "n'importe quoi", compute_signature("mauvais-secret-0123456789", b"x")],
)
async def test_missing_or_invalid_signature_is_rejected(client, dispatcher, sender, repo, signature):
    status, body = await post(client, push_payload(), signature=signature)
    assert (status, body["status"]) == (401, "unauthorized")
    await dispatcher.drain()
    assert sender.sent == []
    assert repo.last_received_at() is None


async def test_signature_of_another_body_is_rejected(client):
    signature = compute_signature(SECRET, encode(issue_payload()))
    status, _ = await post(client, push_payload(), signature=signature)
    assert status == 401


async def test_rejected_signature_does_not_consume_the_delivery(client):
    delivery = str(uuid.uuid4())
    assert (await post(client, push_payload(), delivery=delivery, secret="mauvais-secret-0123456789"))[0] == 401
    assert (await post(client, push_payload(), delivery=delivery)) == (200, {"status": "queued"})


@pytest.mark.parametrize("full_name", ["elbastiano7553/game-project", "ElBastiano7553/game-project-fork", "Autre/game-project", None])
async def test_other_repository_is_rejected(client, dispatcher, sender, full_name):
    payload = push_payload()
    payload["repository"]["full_name"] = full_name
    status, body = await post(client, payload)
    assert (status, body["status"]) == (403, "forbidden_repository")
    await dispatcher.drain()
    assert sender.sent == []


async def test_payload_without_repository_is_rejected(client):
    payload = push_payload()
    del payload["repository"]
    status, body = await post(client, payload)
    assert (status, body["status"]) == (403, "forbidden_repository")


async def test_ping_from_another_repository_is_rejected(client):
    payload = ping_payload()
    payload["repository"]["full_name"] = "Autre/depot"
    assert (await post(client, payload, event="ping"))[0] == 403


async def test_secret_never_appears_in_any_response_or_log(client, caplog):
    for kwargs in ({}, {"secret": "x" * 20}, {"event": "star"}, {"delivery": False}):
        for payload in (push_payload(), b"{oops", {"repository": "x"}):
            body = encode(payload)
            headers = {"Content-Type": "application/json", "X-GitHub-Event": kwargs.get("event", "push"),
                       "X-Hub-Signature-256": compute_signature(kwargs.get("secret", SECRET), body)}
            if kwargs.get("delivery", True) is not False:
                headers["X-GitHub-Delivery"] = str(uuid.uuid4())
            response = await client.post(WEBHOOK_PATH, data=body, headers=headers)
            assert SECRET not in await response.text()
            assert SECRET not in str(response.headers)
    assert SECRET not in caplog.text


def test_app_requires_secret_and_repository(dispatcher):
    with pytest.raises(ValueError):
        create_github_app(secret="", repository=REPOSITORY, dispatcher=dispatcher)
    with pytest.raises(ValueError):
        create_github_app(secret=SECRET, repository="", dispatcher=dispatcher)


# --- Requêtes invalides --------------------------------------------------------


async def test_get_is_not_allowed(client):
    response = await client.get(WEBHOOK_PATH)
    assert response.status == 405


async def test_unknown_path(client):
    response = await client.post("/webhooks/gitlab", data=b"{}")
    assert response.status == 404


async def test_wrong_content_type(client):
    status, body = await post(client, "payload=%7B%7D", content_type="application/x-www-form-urlencoded")
    assert (status, body["status"]) == (415, "unsupported_media_type")


@pytest.mark.parametrize("body", [b"{oops", b"\xff\xfe", b"[1, 2]", b'"texte"'])
async def test_malformed_json(client, body):
    status, data = await post(client, body)
    assert (status, data["status"]) == (400, "invalid_json")


@pytest.mark.parametrize("delivery", [False, "", "a b", "x" * 101, "../../etc"])
async def test_missing_or_invalid_delivery_id(client, delivery):
    status, data = await post(client, push_payload(), delivery=delivery if delivery else False)
    assert (status, data["status"]) == (400, "invalid_delivery")


@pytest.mark.parametrize("event", ["", "Push", "push\nINFO faux", "x" * 61])
async def test_invalid_event_header(client, event):
    status, _ = await post(client, push_payload(), event=event.replace("\n", " "))
    assert status == 400


async def test_incomplete_payload_is_rejected_and_not_stored(client, repo):
    payload = pull_request_payload()
    del payload["pull_request"]
    status, data = await post(client, payload, event="pull_request")
    assert (status, data["status"]) == (400, "invalid_payload")
    assert repo.last_received_at() is None


async def test_body_too_large(client):
    body = b'{"x": "' + b"a" * (MAX_BODY_BYTES + 10) + b'"}'
    status, data = await post(client, body)
    assert (status, data["status"]) == (413, "payload_too_large")


async def test_error_responses_do_not_reflect_large_or_multiline_input(client, caplog):
    payload = push_payload()
    payload["repository"]["full_name"] = "x" * 100_000 + "\nINFO faux journal"
    status, data = await post(client, payload)
    assert status == 403
    assert len(json.dumps(data)) < 300
    assert "faux journal" not in caplog.text


async def test_internal_error_returns_500_without_details(dispatcher, monkeypatch):
    def explode(delivery):
        raise RuntimeError("détail interne secret")

    monkeypatch.setattr(dispatcher, "submit", explode)
    app = create_github_app(secret=SECRET, repository=REPOSITORY, dispatcher=dispatcher)
    async with TestClient(TestServer(app)) as client:
        status, data = await post(client, push_payload())
    assert status == 500
    assert "détail interne" not in json.dumps(data)


async def test_full_queue_returns_503_and_releases_delivery(repo, sender):
    dispatcher = GitHubDispatcher(repo, sender, lambda: {"gitlab_activite": 1}, queue_size=1)
    app = create_github_app(secret=SECRET, repository=REPOSITORY, dispatcher=dispatcher)
    async with TestClient(TestServer(app)) as client:
        assert (await post(client, push_payload()))[0] == 200
        delivery = str(uuid.uuid4())
        status, data = await post(client, issue_payload(), event="issues", delivery=delivery)
        assert (status, data["status"]) == (503, "busy")
        assert repo.get_status(f"issues:{delivery}") is None
        await dispatcher.drain()
        assert (await post(client, issue_payload(), event="issues", delivery=delivery))[1]["status"] == "queued"


# --- Serveur ---------------------------------------------------------------------


async def test_server_start_and_stop(dispatcher, unused_tcp_port):
    app = create_github_app(secret=SECRET, repository=REPOSITORY, dispatcher=dispatcher)
    server = GitHubWebhookServer(app, "127.0.0.1", unused_tcp_port)
    assert await server.start()
    assert server.listening
    assert server.local_url.endswith(WEBHOOK_PATH)
    await server.stop()
    assert not server.listening


async def test_port_already_in_use_is_reported_without_crashing(dispatcher, unused_tcp_port):
    def make():
        return GitHubWebhookServer(create_github_app(secret=SECRET, repository=REPOSITORY, dispatcher=dispatcher), "127.0.0.1", unused_tcp_port)

    first, second = make(), make()
    assert await first.start()
    try:
        assert not await second.start()
        assert "impossible d'écouter" in second.error
        assert not second.listening
    finally:
        await first.stop()


async def test_demo_script_payloads_are_accepted(client, dispatcher, sender):
    from src.github.demo import EVENTS

    expected = {"ping": "pong"}
    for event, build in EVENTS.items():
        status, body = await post(client, build(), event=event)
        assert (status, body["status"]) == (200, expected.get(event, "queued"))
    await dispatcher.drain()
    assert len(sender.sent) == len(EVENTS) - 1
