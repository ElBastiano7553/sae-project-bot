"""Serveur HTTP commun des projets enregistrés (aiohttp, dans le même processus que le bot).

Il n'écoute que sur l'interface locale (``PROJECTS_HTTP_HOST``, 127.0.0.1 par défaut) : rien
n'est visible depuis Internet tant que le propriétaire du serveur n'a pas ajouté, de lui-même,
un site Nginx qui y mène (voir docs/projets-reception.md). Seuls ces chemins existent :

``POST /github/webhook`` (GitHub App, actif seulement si ``GITHUB_APP_WEBHOOK_SECRET`` est défini)
    1. signature ``X-Hub-Signature-256`` présente (sinon 401, sans lire le corps), corps d'au
       plus ``MAX_BODY_BYTES`` (sinon 413), signature HMAC-SHA256 correcte (sinon 401) ;
    2. JSON (sinon 415 / 400), en-têtes ``X-GitHub-Delivery`` et ``X-GitHub-Event`` valides (sinon 400) ;
    3. ``ping`` → 200 ``pong`` ; événement non géré → 200 ``ignored`` ;
    4. le projet est trouvé par l'installation de l'App ET le dépôt indiqués dans l'événement :
       un dépôt relié à aucun projet est ignoré (200).

``POST /gitlab/webhook/<identifiant>`` (un webhook par projet GitLab, créé par l'étape B5)
    1. identifiant bien formé et secret ``X-Gitlab-Token`` dont l'empreinte correspond à celle
       enregistrée pour cet identifiant (sinon 401, même réponse qu'un identifiant inconnu,
       sans lire le corps) ;
    2. JSON d'au plus ``MAX_BODY_BYTES`` (sinon 415 / 413 / 400) ; événement non géré → 200 ``ignored`` ;
    3. le projet de l'événement doit être celui relié à ce webhook (sinon 403).

``GET /github/setup`` et ``GET /github/callback`` (connexion des projets GitHub, étape B4)
    pages de retour après l'installation de la GitHub App et la vérification OAuth, actives
    seulement si la connexion est configurée (voir ``src/providers/github_app.py``).

``GET /gitlab/callback`` (connexion des projets GitLab, étape B5)
    page de retour après l'autorisation OAuth GitLab, qui affiche une seule fois l'adresse et le
    secret du webhook ; active seulement si la connexion est configurée (``src/providers/gitlab_oauth.py``).

Tout autre chemin répond 404, une autre méthode 405. Aucun secret n'apparaît dans les réponses
ni dans les journaux.
"""

from __future__ import annotations

import json
import logging
import re

from aiohttp import web

from src.database.projects import HOOK_ID, ProjectRecord, ProjectRepository
from src.github.events import repository_full_name
from src.github.security import SIGNATURE_HEADER, signature_matches
from src.gitlab.dispatcher import SubmitStatus
from src.gitlab.events import InvalidPayload, project_id_of
from src.gitlab.webhook import delivery_key as gitlab_delivery_key
from src.providers.dispatcher import ProjectDispatcher
from src.providers.github_app import GitHubSetupFlow, add_setup_routes
from src.providers.gitlab_oauth import GitLabSetupFlow, add_gitlab_setup_routes
from src.providers.translate import GITHUB_EVENTS, GITLAB_EVENTS, translate_github, translate_gitlab
from src.services.projects import secret_matches

log = logging.getLogger(__name__)

GITHUB_PATH = "/github/webhook"
GITLAB_PATH = "/gitlab/webhook/{hook_id}"
MAX_BODY_BYTES = 2 * 1024 * 1024
_DELIVERY_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_EVENT_NAME = re.compile(r"^[a-z_]{1,60}$")
# Empreinte comparée quand l'identifiant est inconnu : même durée de traitement qu'un mauvais secret.
_UNKNOWN_HASH = "0" * 64

GITHUB_SECRET_KEY = web.AppKey("projects_github_secret", str)
PROJECTS_KEY = web.AppKey("projects_repository", ProjectRepository)
DISPATCHER_KEY = web.AppKey("projects_dispatcher", ProjectDispatcher)


def create_projects_app(
    *,
    github_secret: str,
    projects: ProjectRepository,
    dispatcher: ProjectDispatcher,
    github_setup: GitHubSetupFlow | None = None,
    gitlab_setup: GitLabSetupFlow | None = None,
) -> web.Application:
    """``github_secret`` vide : le chemin GitHub n'existe pas (404), GitLab reste disponible.

    ``github_setup`` / ``gitlab_setup`` absents : pas de pages de connexion GitHub / GitLab (404).
    """
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[GITHUB_SECRET_KEY] = github_secret
    app[PROJECTS_KEY] = projects
    app[DISPATCHER_KEY] = dispatcher
    if github_secret:
        app.router.add_post(GITHUB_PATH, handle_github)
        if github_setup is not None:
            add_setup_routes(app, github_setup)
    app.router.add_post(GITLAB_PATH, handle_gitlab)
    if gitlab_setup is not None:
        add_gitlab_setup_routes(app, gitlab_setup)
    return app


def _reply(http_status: int, status: str, detail: str = "") -> web.Response:
    body: dict[str, str] = {"status": status}
    if detail:
        body["detail"] = detail
    return web.json_response(body, status=http_status)


async def _read_json(request: web.Request) -> tuple[dict, bytes] | web.Response:
    if request.content_type != "application/json":
        return _reply(415, "unsupported_media_type", "Content-Type attendu : application/json")
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return _reply(413, "payload_too_large", f"taille maximale : {MAX_BODY_BYTES} octets")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reply(400, "invalid_json", "le corps n'est pas un JSON valide")
    if not isinstance(payload, dict):
        return _reply(400, "invalid_json", "le JSON doit être un objet")
    return payload, body


def _submit(request: web.Request, event, targets: list[ProjectRecord], label: str) -> web.Response:
    try:
        status = request.app[DISPATCHER_KEY].submit(event, targets)
    except Exception:
        log.exception("Erreur inattendue en traitant un événement %s", label)
        return _reply(500, "error", "erreur interne, voir logs/bot.log")
    if status is SubmitStatus.BUSY:
        return _reply(503, status, "trop de notifications en attente, réessayez plus tard")
    log.info("Événement %s : %s (%s, %d projet(s))", label, status, event.delivery_key, len(targets))
    return _reply(200, status)


# --- GitHub App ----------------------------------------------------------------------------------


async def handle_github(request: web.Request) -> web.Response:
    signature = request.headers.get(SIGNATURE_HEADER)
    if not signature:
        log.warning("Événement GitHub App refusé : signature absente")
        return _reply(401, "unauthorized", "signature absente")
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return _reply(413, "payload_too_large", f"taille maximale : {MAX_BODY_BYTES} octets")
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return _reply(413, "payload_too_large", f"taille maximale : {MAX_BODY_BYTES} octets")
    if not signature_matches(request.app[GITHUB_SECRET_KEY], body, signature):
        log.warning("Événement GitHub App refusé : signature incorrecte")
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

    if event == "ping":
        return _reply(200, "pong")
    if event not in GITHUB_EVENTS:
        # installation, installation_repositories… : traités à l'étape B6 (désinstallation détectée).
        return _reply(200, SubmitStatus.IGNORED, "événement non géré")
    installation = payload.get("installation")
    installation_id = installation.get("id") if isinstance(installation, dict) else None
    repository = repository_full_name(payload)
    if not isinstance(installation_id, int) or isinstance(installation_id, bool) or not repository:
        return _reply(400, "invalid_payload", "installation ou dépôt absent")
    targets = request.app[PROJECTS_KEY].github_targets(installation_id, repository)
    if not targets:
        log.info("Événement GitHub App ignoré : aucun projet relié (installation %d)", installation_id)
        return _reply(200, SubmitStatus.IGNORED, "aucun projet relié à ce dépôt")
    try:
        translated = translate_github(event, delivery_id, payload)
    except InvalidPayload as exc:
        log.warning("Événement GitHub App %s invalide : %s", event, exc)
        return _reply(400, "invalid_payload", str(exc))
    if translated is None:
        return _reply(200, SubmitStatus.IGNORED, "rien à notifier")
    return _submit(request, translated, targets, f"GitHub App {event}")


# --- GitLab, un webhook par projet ---------------------------------------------------------------


async def handle_gitlab(request: web.Request) -> web.Response:
    hook_id = request.match_info.get("hook_id", "")
    target = request.app[PROJECTS_KEY].gitlab_target(hook_id) if HOOK_ID.match(hook_id) else None
    expected = target[1] if target else _UNKNOWN_HASH
    token_ok = secret_matches(expected, request.headers.get("X-Gitlab-Token"))
    if target is None or not token_ok:
        log.warning("Événement GitLab refusé : webhook inconnu ou secret incorrect")
        return _reply(401, "unauthorized", "secret invalide")
    project = target[0]

    read = await _read_json(request)
    if isinstance(read, web.Response):
        return read
    payload, body = read
    kind = GITLAB_EVENTS.get(request.headers.get("X-Gitlab-Event", ""))
    if kind is None:
        return _reply(200, SubmitStatus.IGNORED, "événement non géré")
    if not _same_gitlab_project(project, payload):
        log.warning("Événement GitLab refusé : projet différent de celui relié au webhook (projet %d)", project.id)
        return _reply(403, "forbidden_project", "projet non autorisé")
    try:
        translated = translate_gitlab(kind, gitlab_delivery_key(request, body), payload)
    except InvalidPayload as exc:
        log.warning("Événement GitLab %s invalide : %s", kind, exc)
        return _reply(400, "invalid_payload", str(exc))
    if translated is None:
        return _reply(200, SubmitStatus.IGNORED, "rien à notifier")
    return _submit(request, translated, [project], f"GitLab {kind}")


def _same_gitlab_project(project: ProjectRecord, payload: dict) -> bool:
    """L'événement concerne-t-il le projet relié (numéro, ou chemin « groupe/projet ») ?"""
    if project.external_id.isdigit():
        return project_id_of(payload) == int(project.external_id)
    data = payload.get("project")
    path = data.get("path_with_namespace") if isinstance(data, dict) else None
    return isinstance(path, str) and path.casefold() == project.external_id


class ProjectsHttpServer:
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
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    async def start(self) -> bool:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except OSError as exc:
            await runner.cleanup()
            self.error = f"impossible d'écouter sur {self.host}:{self.port} ({exc.strerror or exc})"
            log.error("Réception des projets désactivée : %s. Le port est-il déjà utilisé ?", self.error)
            return False
        self._runner = runner
        self.error = None
        log.info("Réception des projets : écoute sur %s", self.local_url)
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
