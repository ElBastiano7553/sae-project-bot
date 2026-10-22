"""Serveur HTTP qui reçoit les webhooks GitHub (aiohttp, dans le même processus que le bot).

Il écoute sur son propre port (``GITHUB_WEBHOOK_PORT``) : le serveur des webhooks
GitLab n'est pas modifié. Contrôles appliqués à chaque requête, dans cet ordre :

1. seule la méthode POST sur ``WEBHOOK_PATH`` est acceptée (sinon 404 / 405) ;
2. l'en-tête ``X-Hub-Signature-256`` doit être présent (sinon 401, sans lire le corps) ;
3. le corps fait au plus ``MAX_BODY_BYTES`` (sinon 413) ;
4. la signature HMAC-SHA256 du corps doit être correcte (sinon 401) ;
5. le corps est du JSON (``Content-Type: application/json``, sinon 415), c'est un
   objet, et ``X-GitHub-Delivery`` / ``X-GitHub-Event`` sont valides (sinon 400) ;
6. le dépôt doit être exactement ``GITHUB_WEBHOOK_REPOSITORY`` (sinon 403) ;
7. une livraison déjà reçue (même ``X-GitHub-Delivery`` ou même fait) n'est pas renotifiée.

L'événement ``ping`` (envoyé par GitHub à la création du webhook) reçoit 200 ``pong``.
Les événements authentifiés mais non gérés reçoivent 200 avec ``"status": "ignored"``.
"""

from __future__ import annotations

import json
import logging
import re

from aiohttp import web

from src.github.dispatcher import GitHubDelivery, GitHubDispatcher
from src.github.events import SUPPORTED_EVENTS, repository_full_name
from src.github.security import SIGNATURE_HEADER, signature_matches
from src.gitlab.dispatcher import SubmitStatus
from src.gitlab.events import InvalidPayload
from src.gitlab.security import describe

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/github"
MAX_BODY_BYTES = 2 * 1024 * 1024
_DELIVERY_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_EVENT_NAME = re.compile(r"^[a-z_]{1,60}$")

SECRET_KEY = web.AppKey("github_secret", str)
REPOSITORY_KEY = web.AppKey("github_repository", str)
DISPATCHER_KEY = web.AppKey("github_dispatcher", GitHubDispatcher)


def create_github_app(*, secret: str, repository: str, dispatcher: GitHubDispatcher) -> web.Application:
    if not secret:
        raise ValueError("Le secret des webhooks GitHub est obligatoire.")
    if not repository:
        raise ValueError("Le dépôt GitHub autorisé est obligatoire.")
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[SECRET_KEY] = secret
    app[REPOSITORY_KEY] = repository
    app[DISPATCHER_KEY] = dispatcher
    app.router.add_post(WEBHOOK_PATH, handle_github_webhook)
    return app


def _reply(http_status: int, status: str, detail: str = "") -> web.Response:
    body: dict[str, str] = {"status": status}
    if detail:
        body["detail"] = detail
    return web.json_response(body, status=http_status)


async def handle_github_webhook(request: web.Request) -> web.Response:
    app = request.app
    signature = request.headers.get(SIGNATURE_HEADER)
    if not signature:
        log.warning("Webhook GitHub refusé : signature absente (origine %s)", request.remote)
        return _reply(401, "unauthorized", "signature absente")
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        log.warning("Webhook GitHub refusé : corps trop volumineux (%d octets annoncés)", request.content_length)
        return _reply(413, "payload_too_large", f"taille maximale : {MAX_BODY_BYTES} octets")
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        log.warning("Webhook GitHub refusé : corps trop volumineux")
        return _reply(413, "payload_too_large", f"taille maximale : {MAX_BODY_BYTES} octets")
    if not signature_matches(app[SECRET_KEY], body, signature):
        log.warning("Webhook GitHub refusé : signature incorrecte (origine %s)", request.remote)
        return _reply(401, "unauthorized", "signature invalide")

    if request.content_type != "application/json":
        return _reply(415, "unsupported_media_type", "Content-Type attendu : application/json")
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
    if not _DELIVERY_ID.match(delivery_id):
        return _reply(400, "invalid_delivery", "en-tête X-GitHub-Delivery absent ou invalide")
    event = request.headers.get("X-GitHub-Event", "").strip()
    if not _EVENT_NAME.match(event):
        return _reply(400, "invalid_event", "en-tête X-GitHub-Event absent ou invalide")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reply(400, "invalid_json", "le corps n'est pas un JSON valide")
    if not isinstance(payload, dict):
        return _reply(400, "invalid_json", "le JSON doit être un objet")

    repository = repository_full_name(payload)
    if repository != app[REPOSITORY_KEY]:
        log.warning("Webhook GitHub refusé : dépôt %s non autorisé", describe(repository, 100))
        return _reply(403, "forbidden_repository", "dépôt non autorisé")

    if event == "ping":
        log.info("Webhook GitHub : ping reçu (livraison %s)", delivery_id)
        return _reply(200, "pong")
    if event not in SUPPORTED_EVENTS:
        log.info("Webhook GitHub ignoré : événement non géré %r", event)
        return _reply(200, SubmitStatus.IGNORED, "événement non géré")

    delivery = GitHubDelivery(event=event, delivery_id=delivery_id, payload=payload)
    try:
        status = app[DISPATCHER_KEY].submit(delivery)
    except InvalidPayload as exc:
        log.warning("Webhook GitHub %s invalide : %s", event, exc)
        return _reply(400, "invalid_payload", str(exc))
    except Exception:
        log.exception("Erreur inattendue en traitant un webhook GitHub %s", event)
        return _reply(500, "error", "erreur interne, voir logs/bot.log")

    if status is SubmitStatus.BUSY:
        return _reply(503, status, "trop de notifications en attente, réessayez plus tard")
    log.info("Webhook GitHub %s : %s (%s)", event, status, delivery.key)
    return _reply(200, status)


class GitHubWebhookServer:
    """Démarre et arrête le serveur. Une erreur de démarrage n'arrête pas le bot."""

    def __init__(self, app: web.Application, host: str, port: int) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.error: str | None = None
        self._runner: web.AppRunner | None = None

    @property
    def listening(self) -> bool:
        return self._runner is not None

    @property
    def local_url(self) -> str:
        return f"http://{self.host}:{self.port}{WEBHOOK_PATH}"

    async def start(self) -> bool:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except OSError as exc:
            await runner.cleanup()
            self.error = f"impossible d'écouter sur {self.host}:{self.port} ({exc.strerror or exc})"
            log.error("Webhooks GitHub désactivés : %s. Le port est-il déjà utilisé ?", self.error)
            return False
        self._runner = runner
        self.error = None
        log.info("Webhooks GitHub : écoute sur %s", self.local_url)
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
