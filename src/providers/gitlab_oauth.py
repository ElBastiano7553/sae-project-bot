"""Connexion des projets GitLab par une application OAuth en lecture seule (multi-projets, étape B5).

Parcours (aucun secret ne passe par Discord) :

1. ``/projet configurer projet:<référence>`` (administrateur, projet GitLab déjà enregistré avec
   ``/projet ajouter``) donne un lien d'autorisation GitLab qui porte un jeton ``state`` signé, à
   usage unique, valable ``STATE_TTL`` et lié à ce serveur Discord, à cette personne et à CE projet ;
2. GitLab renvoie la personne sur ``GET /gitlab/callback`` : Samae vérifie le jeton, échange le code
   contre un accès temporaire, demande à GitLab le rôle de cette personne sur ce projet (Maintainer
   ou Owner exigé), puis révoque aussitôt cet accès, même en cas d'échec ;
3. Samae crée alors l'identifiant public du webhook et son secret, n'enregistre que l'empreinte
   SHA-256 du secret (``provider_links``), et affiche une seule fois, sur cette page, l'adresse et le
   secret à coller dans GitLab (Paramètres → Webhooks). Recharger la page ne les affiche plus.

La réception (B3, ``POST /gitlab/webhook/<identifiant>``) accepte ensuite les événements de ce projet
seulement. Samae ne garde aucun token GitLab et n'a aucun droit d'écriture : il ne crée pas le
webhook lui-même. Les briques communes avec GitHub (jetons ``state``, pages) viennent de
``github_app.py``.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import aiohttp
from aiohttp import web

from src.database.gitlab_setup import GitlabSetupRepository
from src.database.projects import ProjectRepository
from src.providers.github_app import _HEADERS, _PAGE, SetupStates, page
from src.services.projects import ProjectError, hash_secret

log = logging.getLogger(__name__)

CALLBACK_PATH = "/gitlab/callback"
STEP = "gitlab"
STATE_PURPOSE = b"samae:gitlab-setup-state"
# Lecture seule : « read_user » pour connaître la personne, « read_api » pour lire son rôle sur le projet.
SCOPES = "read_user read_api"
# Niveaux d'accès GitLab : 40 = Maintainer, 50 = Owner. Developer (30) ne suffit pas.
MAINTAINER = 40
ACCESS_LABELS = {10: "Guest", 15: "Planner", 20: "Reporter", 30: "Developer", 40: "Maintainer", 50: "Owner"}
WEBHOOK_PATH_PREFIX = "/gitlab/webhook/"

_CODE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_PATH = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.][A-Za-z0-9_.-]*)+$")


class GitLabAccessError(Exception):
    """GitLab a refusé (code expiré, autorisation annulée, projet invisible). Jamais de token dans le message."""


class GitLabUnavailable(Exception):
    """GitLab injoignable, limite d'appels atteinte ou réponse inattendue."""


@dataclass(frozen=True)
class ProjectAccess:
    project_id: int  # numéro du projet chez GitLab
    path: str  # « groupe/projet »
    access_level: int  # rôle de la personne sur ce projet (0 : aucun)
    username: str


def _level(permissions: object, key: str) -> int:
    data = permissions.get(key) if isinstance(permissions, dict) else None
    level = data.get("access_level") if isinstance(data, dict) else None
    return level if isinstance(level, int) and not isinstance(level, bool) else 0


class GitLabUserClient:
    """Échange du code OAuth, lecture du rôle sur un projet, révocation. Rien n'est gardé."""

    def __init__(self, base_url: str, client_id: str, client_secret: str, *, timeout: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=self._timeout, headers={"User-Agent": "Samae-bot"})

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        params = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state, "scope": SCOPES}
        return f"{self.base_url}/oauth/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        data = {
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        try:
            async with self._session() as session:
                async with session.post(f"{self.base_url}/oauth/token", data=data, headers={"Accept": "application/json"}) as response:
                    if response.status in (400, 401):
                        raise GitLabAccessError("code refusé")
                    if response.status != 200:
                        raise GitLabUnavailable(f"échange du code : HTTP {response.status}")
                    payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise GitLabUnavailable(f"échange du code : {type(exc).__name__}") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GitLabAccessError("code refusé")
        return token

    async def _get(self, session: aiohttp.ClientSession, token: str, path: str) -> dict | None:
        """Objet JSON, ou None si GitLab répond 403 ou 404 (projet invisible pour cette personne)."""
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        async with session.get(f"{self.base_url}/api/v4{path}", headers=headers) as response:
            if response.status == 401:
                raise GitLabAccessError("accès refusé par GitLab (HTTP 401)")
            if response.status in (403, 404):
                return None
            if response.status == 429:
                raise GitLabUnavailable(f"{path.split('/')[1]} : limite d'appels de GitLab atteinte (HTTP 429)")
            if response.status != 200:
                raise GitLabUnavailable(f"{path.split('/')[1]} : HTTP {response.status}")
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise GitLabUnavailable("réponse inattendue")
        return payload

    async def project_access(self, token: str, project_ref: str) -> ProjectAccess | None:
        """Rôle de la personne sur ce projet (numéro ou « groupe/projet »), ou None s'il lui est invisible.

        Le rôle donné par le projet ne compte que l'appartenance directe au projet ou à son groupe : s'il
        est inférieur à Maintainer, Samae lit aussi le rôle hérité d'un groupe parent (``members/all``).
        """
        encoded = quote(project_ref, safe="")
        try:
            async with self._session() as session:
                user = await self._get(session, token, "/user")
                project = await self._get(session, token, f"/projects/{encoded}")
                if user is None or project is None:
                    return None
                project_id, path, username = project.get("id"), project.get("path_with_namespace"), user.get("username")
                user_id = user.get("id")
                if not isinstance(project_id, int) or not isinstance(path, str) or not _PATH.match(path):
                    raise GitLabUnavailable("projet : réponse inattendue")
                permissions = project.get("permissions")
                level = max(_level(permissions, "project_access"), _level(permissions, "group_access"))
                if level < MAINTAINER and isinstance(user_id, int):
                    member = await self._get(session, token, f"/projects/{project_id}/members/all/{user_id}")
                    inherited = member.get("access_level") if member else None
                    if isinstance(inherited, int) and not isinstance(inherited, bool):
                        level = max(level, inherited)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise GitLabUnavailable(f"lecture du projet : {type(exc).__name__}") from exc
        return ProjectAccess(project_id, path, level, username if isinstance(username, str) else "?")

    async def revoke(self, token: str) -> None:
        """Révoque l'accès temporaire (au mieux : un échec est seulement noté, il expire de toute façon)."""
        data = {"client_id": self.client_id, "client_secret": self._client_secret, "token": token}
        try:
            async with self._session() as session:
                async with session.post(f"{self.base_url}/oauth/revoke", data=data) as response:
                    if response.status != 200:
                        log.warning("Révocation de l'accès GitLab temporaire : HTTP %d", response.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Révocation de l'accès GitLab temporaire impossible (%s)", type(exc).__name__)


def same_project(external_id: str, access: ProjectAccess) -> bool:
    """Le projet lu chez GitLab est-il bien celui enregistré (numéro, ou chemin sans tenir compte de la casse) ?"""
    if external_id.isdigit():
        return int(external_id) == access.project_id
    return external_id == access.path.casefold()


_RESTART = "Relancez /projet configurer dans Discord pour obtenir un nouveau lien."


def _secret_page(webhook_url: str, secret: str, path: str, replaced: bool) -> web.Response:
    """Seule page qui montre le secret du webhook. Échappée, sans cache, sans référent."""
    replaced_note = (
        "<p><strong>Ce projet avait déjà un webhook Samae : son ancien secret ne fonctionne plus.</strong> "
        "Modifiez le webhook existant dans GitLab au lieu d'en ajouter un second.</p>"
        if replaced
        else ""
    )
    body = (
        f"<p>Le projet <strong>{html.escape(path)}</strong> est relié à Samae. Il reste à créer son webhook dans GitLab.</p>"
        f"{replaced_note}"
        "<p><strong>Notez ces deux valeurs maintenant : le secret ne sera plus jamais affiché.</strong> "
        "Samae n'en garde qu'une empreinte et ne peut pas vous le redonner.</p>"
        f"<p>URL : <code>{html.escape(webhook_url)}</code></p>"
        f"<p>Jeton secret : <code>{html.escape(secret)}</code></p>"
        "<ol><li>Dans GitLab, ouvrez le projet → Paramètres → Webhooks → Ajouter un webhook.</li>"
        "<li>Collez l'URL et le jeton secret ci-dessus.</li>"
        "<li>Déclencheurs : événements de poussée, de merge request, de pipeline et de déploiement.</li>"
        "<li>Laissez la vérification SSL activée, puis enregistrez.</li>"
        "<li>Bouton « Tester » → « Événements de poussée » : la notification arrive dans le salon « Code » du projet.</li></ol>"
        "<p>Retournez ensuite sur Discord. Vous pouvez fermer cette page.</p>"
    )
    return web.Response(
        status=200, text=_PAGE.format(title="Projet relié", body=body), content_type="text/html", charset="utf-8", headers=_HEADERS
    )


class GitLabSetupFlow:
    """Lien d'autorisation et page ``/gitlab/callback``."""

    def __init__(
        self,
        *,
        public_url: str,
        instance_url: str,
        states: SetupStates,
        client: GitLabUserClient,
        projects: ProjectRepository,
        legacy_project_id=lambda guild_id: None,
        on_linked=None,
    ) -> None:
        """``legacy_project_id(guild_id)`` : projet du webhook historique du ``.env`` encore actif sur ce serveur, ou None.

        ``on_linked(guild_id, user_id, project_id, replaced)`` : appelé après l'enregistrement du lien (journal d'administration).
        """
        self.public_url = public_url.rstrip("/")
        self.instance_url = instance_url.rstrip("/")
        self.states = states
        self.client = client
        self.projects = projects
        self._legacy_project_id = legacy_project_id
        self._on_linked = on_linked

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}{CALLBACK_PATH}"

    def webhook_url(self, hook_id: str) -> str:
        return f"{self.public_url}{WEBHOOK_PATH_PREFIX}{hook_id}"

    def accepts(self, instance_url: str) -> bool:
        """Le projet est-il sur l'instance de l'application OAuth ?"""
        return instance_url.rstrip("/").casefold() == self.instance_url.casefold()

    def authorize_url(self, guild_id: int, user_id: int, project_id: int) -> str:
        return self.client.authorize_url(self.redirect_uri, self.states.issue(STEP, guild_id, user_id, project_id=project_id))

    async def handle_callback(self, request: web.Request) -> web.Response:
        query = request.query
        claim = self.states.consume(STEP, query.get("state"))
        if claim is None:
            log.warning("Retour d'autorisation GitLab refusé : lien inconnu, expiré ou déjà utilisé")
            return page(400, "Lien expiré", "Ce lien d'autorisation est inconnu, expiré (15 minutes) ou déjà utilisé.", _RESTART)
        project = self.projects.get(claim.guild_id, claim.project_id)
        if project is None or project.provider != "gitlab" or not self.accepts(project.instance_url):
            return page(400, "Projet introuvable", "Ce projet GitLab n'est plus suivi sur ce serveur : rien n'a été relié.", _RESTART)
        if "error" in query:
            return page(400, "Autorisation annulée", "GitLab n'a pas donné l'autorisation : rien n'a été relié.", _RESTART)
        code = query.get("code", "")
        if not _CODE.match(code):
            return page(400, "Autorisation invalide", "GitLab n'a pas renvoyé de code valide : rien n'a été relié.", _RESTART)
        try:
            token = await self.client.exchange_code(code, self.redirect_uri)
        except GitLabAccessError as exc:
            log.warning("Autorisation GitLab refusée (%s)", exc)
            return page(400, "Autorisation refusée", "GitLab a refusé l'autorisation (code expiré ou déjà utilisé).", _RESTART)
        except GitLabUnavailable as exc:
            log.warning("GitLab injoignable pendant la connexion (%s)", exc)
            return page(502, "GitLab injoignable", "GitLab n'a pas répondu correctement. Réessayez dans quelques minutes.", _RESTART)
        try:
            access = await self.client.project_access(token, project.external_id)
        except GitLabAccessError as exc:
            log.warning("Lecture du projet GitLab %d refusée (%s)", project.id, exc)
            access = None
        except GitLabUnavailable as exc:
            log.warning("GitLab injoignable pendant la vérification (%s)", exc)
            return page(502, "GitLab injoignable", "GitLab n'a pas répondu correctement. Réessayez dans quelques minutes.", _RESTART)
        finally:
            await self.client.revoke(token)
            del token
        if access is None or not same_project(project.external_id, access):
            log.warning("Projet GitLab %d refusé : invisible pour la personne connectée (serveur %d)", project.id, claim.guild_id)
            return page(
                403, "Accès refusé",
                "Le compte GitLab connecté ne voit pas ce projet : rien n'a été relié.",
                "Connectez-vous avec un compte Maintainer du projet, puis " + _RESTART.lower(),
            )
        if access.access_level < MAINTAINER:
            role = ACCESS_LABELS.get(access.access_level, "aucun rôle")
            log.warning("Projet GitLab %d refusé : rôle %s insuffisant (serveur %d)", project.id, role, claim.guild_id)
            return page(
                403, "Droits insuffisants",
                f"Votre rôle sur ce projet est « {role} » : il faut Maintainer ou Owner pour le relier. Rien n'a été relié.",
                "Demandez à un Maintainer de lancer /projet configurer, ou connectez-vous avec un tel compte.",
            )
        if self._legacy_project_id(claim.guild_id) == access.project_id:
            return page(
                409, "Déjà suivi par le .env",
                "Ce projet est déjà suivi par le webhook historique du .env (projet « env-gitlab »). Le relier aussi doublerait "
                "ses notifications : videz d'abord GITLAB_WEBHOOK_SECRET sur le serveur qui héberge Samae. Rien n'a été relié.",
            )
        replaced = self.projects.link(claim.guild_id, project.id) is not None
        hook_id, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        try:
            self.projects.link_gitlab(claim.guild_id, project.id, hook_id, hash_secret(secret))
        except ProjectError:
            log.exception("Projet GitLab %d : lien impossible à enregistrer", project.id)
            return page(500, "Erreur", "Le projet n'a pas pu être relié. Réessayez.", _RESTART)
        log.info(
            "Projet GitLab %d relié pour le serveur %d (rôle %s, webhook %s)",
            project.id, claim.guild_id, ACCESS_LABELS.get(access.access_level, "?"), "remplacé" if replaced else "créé",
        )
        if self._on_linked is not None:
            try:
                self._on_linked(claim.guild_id, claim.user_id, project.id, replaced)
            except Exception:  # le journal ne doit jamais empêcher d'afficher le secret, déjà enregistré
                log.exception("Journal d'administration : connexion GitLab du projet %d non notée", project.id)
        return _secret_page(self.webhook_url(hook_id), secret, access.path, replaced)


def add_gitlab_setup_routes(app: web.Application, flow: GitLabSetupFlow) -> None:
    app.router.add_get(CALLBACK_PATH, flow.handle_callback, allow_head=False)


def build_gitlab_flow(
    settings,
    repository: GitlabSetupRepository,
    projects: ProjectRepository,
    *,
    clock=None,
    base_url: str | None = None,
    on_linked=None,
    home_guild_id=None,
) -> GitLabSetupFlow:
    """Parcours à partir des réglages (``settings.gitlab_app_enabled`` doit être vrai).

    ``base_url`` (tests) : adresse où envoyer les requêtes, à la place de l'instance ``GITLAB_APP_URL``.
    """
    states = SetupStates(repository, settings.gitlab_app_client_secret, purpose=STATE_PURPOSE, **({"clock": clock} if clock else {}))

    # Serveur principal (projet GitLab historique du .env) : DISCORD_GUILD_ID, ou celui choisi avec /setup.
    home = home_guild_id or (lambda: settings.guild_id)

    def legacy_project_id(guild_id: int) -> int | None:
        if guild_id == home() and settings.gitlab_webhooks_enabled:
            return settings.gitlab_project_id
        return None

    return GitLabSetupFlow(
        public_url=settings.projects_public_url,
        instance_url=settings.gitlab_app_url,
        states=states,
        client=GitLabUserClient(base_url or settings.gitlab_app_url, settings.gitlab_app_client_id, settings.gitlab_app_client_secret),
        projects=projects,
        legacy_project_id=legacy_project_id,
        on_linked=on_linked,
    )
