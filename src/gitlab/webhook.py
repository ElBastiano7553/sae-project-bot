"""Serveur HTTP qui reçoit les webhooks GitLab (aiohttp, dans le même processus que le bot).

Contrôles appliqués à chaque requête, dans cet ordre :

1. seule la méthode POST sur ``WEBHOOK_PATH`` est acceptée (sinon 404 / 405) ;
2. l'en-tête ``X-Gitlab-Token`` doit correspondre au secret (comparaison à temps
   constant), avant même de lire le corps (sinon 401) ;
3. le corps est du JSON (``Content-Type: application/json``, sinon 415), d'au
   plus ``MAX_BODY_BYTES`` (sinon 413), et c'est un objet (sinon 400) ;
4. le projet doit être celui de ``GITLAB_PROJECT_ID`` s'il est défini (sinon 403) ;
5. un événement déjà reçu (même clé de livraison) n'est pas renotifié.

Les événements authentifiés mais non gérés (Issues, tags…) reçoivent 200 avec
``"status": "ignored"`` : GitLab désactive les webhooks qui échouent souvent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from aiohttp import web

from src.gitlab.dispatcher import IncomingEvent, NotificationDispatcher, SubmitStatus
from src.gitlab.events import SUPPORTED_EVENTS, InvalidPayload, project_id_of
from src.gitlab.security import token_matches

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/gitlab"
MAX_BODY_BYTES = 2 * 1024 * 1024
_DELIVERY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")

SECRET_KEY = web.AppKey("gitlab_secret", str)
PROJECT_KEY = web.AppKey("gitlab_project_id", object)
DISPATCHER_KEY = web.AppKey("gitlab_dispatcher", NotificationDispatcher)


def create_webhook_app(*, secret: str, project_id: int | None, dispatcher: NotificationDispatcher) -> web.Application:
    if not secret:
        raise ValueError("Le secret des webhooks GitLab est obligatoire.")
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[SECRET_KEY] = secret
    app[PROJECT_KEY] = project_id
    app[DISPATCHER_KEY] = dispatcher
    app.router.add_post(WEBHOOK_PATH, handle_gitlab_webhook)
    return app


def _reply(http_status: int, status: str, detail: str = "") -> web.Response:
    body: dict[str, str] = {"status": status}
    if detail:
        body["detail"] = detail
    return web.json_response(body, status=http_status)


def delivery_key(request: web.Request, body: bytes) -> str:
    """Identifiant de la livraison : stable si GitLab renvoie le même événement."""
    for header in ("Idempotency-Key", "X-Gitlab-Event-UUID"):
        value = request.headers.get(header, "").strip()
        if value and _DELIVERY_KEY.match(value):
            return value
    return "sha256-" + hashlib.sha256(body).hexdigest()


async def handle_gitlab_webhook(request: web.Request) -> web.Response:
    app = request.app
    if not token_matches(app[SECRET_KEY], request.headers.get("X-Gitlab-Token")):
        log.warning("Webhook GitLab refusé : secret absent ou incorrect (origine %s)", request.remote)
        return _reply(401, "unauthorized", "secret invalide")

    if request.content_type != "application/json":
        return _reply(415, "unsupported_media_type", "Content-Type attendu : application/json")
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        log.warning("Webhook GitLab refusé : corps trop volumineux")
        return _reply(413, "payload_too_large", f"taille maximale : {MAX_BODY_BYTES} octets")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reply(400, "invalid_json", "le corps n'est pas un JSON valide")
    if not isinstance(payload, dict):
        return _reply(400, "invalid_json", "le JSON doit être un objet")

    header_event = request.headers.get("X-Gitlab-Event", "")
    kind = SUPPORTED_EVENTS.get(header_event)
    if kind is None:
        log.info("Webhook GitLab ignoré : événement non géré %r", header_event[:50])
        return _reply(200, SubmitStatus.IGNORED, "événement non géré")

    expected_project = app[PROJECT_KEY]
    if expected_project is not None and project_id_of(payload) != expected_project:
        log.warning("Webhook GitLab refusé : projet %s différent de GITLAB_PROJECT_ID", project_id_of(payload))
        return _reply(403, "forbidden_project", "projet non autorisé")

    event = IncomingEvent(kind=kind, delivery_key=delivery_key(request, body), payload=payload)
    try:
        status = app[DISPATCHER_KEY].submit(event)
    except InvalidPayload as exc:
        log.warning("Webhook GitLab %s invalide : %s", kind, exc)
        return _reply(400, "invalid_payload", str(exc))
    except Exception:
        log.exception("Erreur inattendue en traitant un webhook GitLab %s", kind)
        return _reply(500, "error", "erreur interne, voir logs/bot.log")

    if status is SubmitStatus.BUSY:
        return _reply(503, status, "trop de notifications en attente, réessayez plus tard")
    log.info("Webhook GitLab %s : %s (%s)", kind, status, event.event_key)
    return _reply(200, status)


class GitLabWebhookServer:
    """Démarre et arrête le serveur. Une erreur de démarrage n'arrête pas le bot (la V1 continue)."""

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
            log.error("Webhooks GitLab désactivés : %s. Le port est-il déjà utilisé ?", self.error)
            return False
        self._runner = runner
        self.error = None
        log.info("Webhooks GitLab : écoute sur %s", self.local_url)
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
