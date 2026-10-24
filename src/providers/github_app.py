"""Connexion des projets GitHub par la GitHub App (multi-projets, étape B4).

Parcours (aucun secret ne passe par Discord) :

1. ``/projet configurer`` (administrateur) donne un lien d'installation de l'App qui porte un
   jeton ``state`` signé, à usage unique, valable ``STATE_TTL`` et lié à ce serveur Discord et
   à cette personne ;
2. après l'installation, GitHub renvoie la personne sur ``GET /github/setup`` avec le numéro
   d'installation : Samae vérifie le jeton, puis l'envoie s'identifier sur GitHub (OAuth) avec
   un second jeton, lié en plus à ce numéro d'installation ;
3. ``GET /github/callback`` : Samae vérifie ce second jeton, échange le code contre un accès
   temporaire de la personne, demande à GitHub si cette personne a bien accès à l'installation
   et sur quels dépôts elle a le rôle Admin ou Maintain, révoque aussitôt cet accès (même en cas
   d'échec) et ne garde que le numéro d'installation, le compte et ces dépôts, pour CE serveur
   Discord. Les jetons sont supprimés dès leur utilisation ; rien n'est enregistré en cas d'échec ;
4. de retour sur Discord, l'administrateur choisit le dépôt : le projet est relié à
   l'installation et la réception (B3) l'accepte.

Sans l'étape 3, quelqu'un pourrait relier l'installation d'un autre en changeant le numéro dans
l'adresse. Samae ne détient aucune clé privée de l'App et n'a aucun droit d'écriture sur GitHub.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from src.database.github_setup import GithubSetupRepository, StateClaim, utcnow

log = logging.getLogger(__name__)

SETUP_PATH = "/github/setup"
CALLBACK_PATH = "/github/callback"
STATE_TTL = timedelta(minutes=15)
GITHUB_URL = "https://github.com"
GITHUB_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
# 100 éléments par page, 10 pages au plus : 1 000 installations ou dépôts.
PAGE_SIZE = 100
MAX_PAGES = 10

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32}\.[A-Za-z0-9_-]{43}$")
_INSTALLATION_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")


# --- Jetons « state » ------------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SetupStates:
    """Jetons ``state`` : aléatoires, signés (HMAC-SHA256), à usage unique, expirent après ``STATE_TTL``.

    La base ne garde que leur empreinte : la lire ne permet pas de fabriquer un lien valide.
    La clé de signature est dérivée du secret OAuth de l'App (jamais affiché).
    """

    def __init__(self, repository: GithubSetupRepository, client_secret: str, *, clock=utcnow) -> None:
        self._repo = repository
        self._key = hmac.new(client_secret.encode("utf-8"), b"samae:github-setup-state", hashlib.sha256).digest()
        self._clock = clock

    def _signature(self, step: str, nonce: str) -> str:
        digest = hmac.new(self._key, f"{step}:{nonce}".encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def issue(self, step: str, guild_id: int, user_id: int, *, installation_id: int | None = None) -> str:
        nonce = secrets.token_urlsafe(24)
        token = f"{nonce}.{self._signature(step, nonce)}"
        self._repo.save_state(
            _hash(token), step, guild_id, user_id, expires_at=self._clock() + STATE_TTL, installation_id=installation_id
        )
        return token

    def consume(self, step: str, token: str | None) -> StateClaim | None:
        if not token or not _TOKEN.match(token):
            return None
        nonce, signature = token.split(".")
        if not hmac.compare_digest(signature, self._signature(step, nonce)):
            return None
        return self._repo.consume_state(_hash(token), step, now=self._clock())


# --- Appels à GitHub, avec l'accès temporaire de la personne -------------------------------------


class GitHubAccessError(Exception):
    """GitHub a refusé (code expiré, autorisation annulée…). Le message ne contient jamais de token."""


class GitHubUnavailable(Exception):
    """GitHub injoignable ou réponse inattendue."""


@dataclass(frozen=True)
class InstallationAccess:
    installation_id: int
    account: str
    repositories: tuple[str, ...]  # dépôts que la personne administre ou maintient (seuls reliables)
    others: int = 0  # dépôts de l'installation où elle n'a que des droits de lecture ou d'écriture


# Droits GitHub qui permettent de relier un dépôt (comme « Maintainer » côté GitLab) : administrer
# ou maintenir le dépôt. Lire ou pousser du code ne suffit pas.
MANAGE_PERMISSIONS = ("admin", "maintain")


def _can_manage(repository: dict) -> bool:
    permissions = repository.get("permissions")
    return isinstance(permissions, dict) and any(permissions.get(name) is True for name in MANAGE_PERMISSIONS)


class GitHubUserClient:
    """Échange du code OAuth, vérification de l'accès à une installation, révocation. Rien n'est gardé."""

    def __init__(
        self, client_id: str, client_secret: str, *, github_url: str = GITHUB_URL, api_url: str = GITHUB_API_URL, timeout: float = 10
    ) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self.github_url = github_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=self._timeout, headers={"User-Agent": "Samae-bot"})

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        data = {"client_id": self.client_id, "client_secret": self._client_secret, "code": code, "redirect_uri": redirect_uri}
        try:
            async with self._session() as session:
                async with session.post(f"{self.github_url}/login/oauth/access_token", data=data, headers={"Accept": "application/json"}) as response:
                    if response.status != 200:
                        raise GitHubUnavailable(f"échange du code : HTTP {response.status}")
                    payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise GitHubUnavailable(f"échange du code : {type(exc).__name__}") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise GitHubAccessError(str(error) if isinstance(error, str) and error.isidentifier() else "code refusé")
        return token

    async def _pages(self, session: aiohttp.ClientSession, token: str, path: str, key: str) -> list[dict]:
        items: list[dict] = []
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        for page in range(1, MAX_PAGES + 1):
            async with session.get(f"{self.api_url}{path}", params={"per_page": PAGE_SIZE, "page": page}, headers=headers) as response:
                if response.status in (401, 403, 404):
                    raise GitHubAccessError(f"accès refusé par GitHub (HTTP {response.status})")
                if response.status != 200:
                    raise GitHubUnavailable(f"{path} : HTTP {response.status}")
                payload = await response.json(content_type=None)
            batch = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise GitHubUnavailable(f"{path} : réponse inattendue")
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < PAGE_SIZE:
                break
        return items

    async def installation_access(self, token: str, installation_id: int) -> InstallationAccess | None:
        """L'installation si cette personne y a accès (avec ses dépôts), sinon None."""
        try:
            async with self._session() as session:
                installations = await self._pages(session, token, "/user/installations", "installations")
                found = next((i for i in installations if i.get("id") == installation_id), None)
                if found is None:
                    return None
                owner = found.get("account")
                account = owner.get("login") if isinstance(owner, dict) else None
                repositories = await self._pages(session, token, f"/user/installations/{installation_id}/repositories", "repositories")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise GitHubUnavailable(f"vérification de l'installation : {type(exc).__name__}") from exc
        valid = [r for r in repositories if isinstance(r.get("full_name"), str) and _REPOSITORY.match(r["full_name"])]
        managed = tuple(r["full_name"] for r in valid if _can_manage(r))
        if not isinstance(account, str) or not _ACCOUNT.match(account):
            account = "?"
        return InstallationAccess(installation_id, account, managed, others=len(valid) - len(managed))

    async def revoke(self, token: str) -> None:
        """Révoque l'accès temporaire (au mieux : un échec est seulement noté, il expire de toute façon)."""
        credentials = base64.b64encode(f"{self.client_id}:{self._client_secret}".encode("utf-8")).decode("ascii")
        headers = {"Authorization": f"Basic {credentials}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        try:
            async with self._session() as session:
                async with session.delete(
                    f"{self.api_url}/applications/{self.client_id}/token", json={"access_token": token}, headers=headers
                ) as response:
                    if response.status not in (204, 404):
                        log.warning("Révocation de l'accès GitHub temporaire : HTTP %d", response.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Révocation de l'accès GitHub temporaire impossible (%s)", type(exc).__name__)


# --- Pages de retour ----------------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Samae · {title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem;line-height:1.5;color:#1f2328}}
h1{{font-size:1.4rem}}</style></head>
<body><h1>{title}</h1>{body}</body></html>"""

# En-têtes de toutes les pages : rien en cache, aucune adresse (avec ses jetons) transmise ailleurs.
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}


def page(status: int, title: str, *paragraphs: str) -> web.Response:
    """Page HTML simple. Les paragraphes sont échappés : rien de reçu n'est interprété."""
    body = "".join(f"<p>{html.escape(text)}</p>" for text in paragraphs)
    return web.Response(
        status=status, text=_PAGE.format(title=html.escape(title), body=body), content_type="text/html", charset="utf-8", headers=_HEADERS
    )


_RESTART = "Relancez /projet configurer dans Discord pour obtenir un nouveau lien."


class GitHubSetupFlow:
    """Lien d'installation, pages ``/github/setup`` et ``/github/callback``."""

    def __init__(
        self,
        *,
        app_slug: str,
        public_url: str,
        states: SetupStates,
        client: GitHubUserClient,
        installations: GithubSetupRepository,
    ) -> None:
        self.app_slug = app_slug
        self.public_url = public_url.rstrip("/")
        self.states = states
        self.client = client
        self.installations = installations

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}{CALLBACK_PATH}"

    def install_url(self, guild_id: int, user_id: int) -> str:
        token = self.states.issue("install", guild_id, user_id)
        return f"{self.client.github_url}/apps/{self.app_slug}/installations/new?{urlencode({'state': token})}"

    async def handle_setup(self, request: web.Request) -> web.Response:
        query = request.query
        if query.get("setup_action") == "request":
            return page(
                200, "Demande envoyée",
                "La demande d'installation a été transmise à un propriétaire du compte GitHub.",
                "Une fois qu'il l'a acceptée, " + _RESTART.lower(),
            )
        raw_id = query.get("installation_id", "")
        claim = self.states.consume("install", query.get("state"))
        if claim is None:
            log.warning("Retour d'installation GitHub refusé : lien inconnu, expiré ou déjà utilisé")
            return page(400, "Lien expiré", "Ce lien d'installation est inconnu, expiré (15 minutes) ou déjà utilisé.", _RESTART)
        if not _INSTALLATION_ID.match(raw_id):
            return page(400, "Installation inconnue", "GitHub n'a pas indiqué d'installation valide.", _RESTART)
        installation_id = int(raw_id)
        token = self.states.issue("oauth", claim.guild_id, claim.user_id, installation_id=installation_id)
        params = {"client_id": self.client.client_id, "redirect_uri": self.redirect_uri, "state": token, "allow_signup": "false"}
        log.info("Installation GitHub %d : vérification de l'accès demandée (serveur %d)", installation_id, claim.guild_id)
        raise web.HTTPFound(f"{self.client.github_url}/login/oauth/authorize?{urlencode(params)}", headers=_HEADERS)

    async def handle_callback(self, request: web.Request) -> web.Response:
        query = request.query
        claim = self.states.consume("oauth", query.get("state"))
        if claim is None or claim.installation_id is None:
            log.warning("Retour d'autorisation GitHub refusé : lien inconnu, expiré ou déjà utilisé")
            return page(400, "Lien expiré", "Ce lien d'autorisation est inconnu, expiré (15 minutes) ou déjà utilisé.", _RESTART)
        if "error" in query:
            return page(400, "Autorisation annulée", "GitHub n'a pas donné l'autorisation : rien n'a été relié.", _RESTART)
        code = query.get("code", "")
        if not _CODE.match(code):
            return page(400, "Autorisation invalide", "GitHub n'a pas renvoyé de code valide : rien n'a été relié.", _RESTART)
        installation_id = claim.installation_id
        try:
            token = await self.client.exchange_code(code, self.redirect_uri)
        except GitHubAccessError as exc:
            log.warning("Autorisation GitHub refusée (%s)", exc)
            return page(400, "Autorisation refusée", "GitHub a refusé l'autorisation (code expiré ou déjà utilisé).", _RESTART)
        except GitHubUnavailable as exc:
            log.warning("GitHub injoignable pendant la connexion (%s)", exc)
            return page(502, "GitHub injoignable", "GitHub n'a pas répondu correctement. Réessayez dans quelques minutes.", _RESTART)
        try:
            access = await self.client.installation_access(token, installation_id)
        except GitHubAccessError as exc:
            log.warning("Vérification de l'installation GitHub %d refusée (%s)", installation_id, exc)
            access = None
        except GitHubUnavailable as exc:
            log.warning("GitHub injoignable pendant la vérification (%s)", exc)
            return page(502, "GitHub injoignable", "GitHub n'a pas répondu correctement. Réessayez dans quelques minutes.", _RESTART)
        finally:
            await self.client.revoke(token)
            del token
        if access is None:
            log.warning("Installation GitHub %d refusée : la personne connectée n'y a pas accès (serveur %d)", installation_id, claim.guild_id)
            return page(
                403, "Accès refusé",
                "Le compte GitHub connecté n'a pas accès à cette installation de l'App : rien n'a été relié.",
                "Connectez-vous avec un compte qui gère ce dépôt, puis " + _RESTART.lower(),
            )
        if not access.repositories:
            log.warning(
                "Installation GitHub %d refusée : la personne connectée n'administre aucun de ses dépôts (serveur %d)",
                installation_id, claim.guild_id,
            )
            return page(
                403, "Droits insuffisants",
                "Le compte GitHub connecté n'administre ni ne maintient aucun dépôt de cette installation : rien n'a été relié.",
                "Il faut le rôle Admin ou Maintain sur le dépôt. Connectez-vous avec un tel compte, puis " + _RESTART.lower(),
            )
        self.installations.save_installation(
            claim.guild_id, installation_id, access.account, access.repositories, authorized_by=claim.user_id
        )
        log.info(
            "Installation GitHub %d vérifiée pour le serveur %d (%d dépôt(s))", installation_id, claim.guild_id, len(access.repositories)
        )
        count = len(access.repositories)
        ignored = f" {access.others} autre(s) dépôt(s), où vous n'avez pas le rôle Admin ou Maintain, ne seront pas proposés." if access.others else ""
        return page(
            200, "Installation vérifiée",
            f"L'App est installée sur le compte {access.account}. Vous pourrez relier {count} dépôt(s) que vous administrez.{ignored}",
            "Retournez sur Discord et cliquez sur « Choisir le dépôt » (ou relancez /projet configurer).",
            "Vous pouvez fermer cette page.",
        )


def add_setup_routes(app: web.Application, flow: GitHubSetupFlow) -> None:
    app.router.add_get(SETUP_PATH, flow.handle_setup, allow_head=False)
    app.router.add_get(CALLBACK_PATH, flow.handle_callback, allow_head=False)


def build_flow(settings, repository: GithubSetupRepository, *, clock=utcnow, github_url: str = GITHUB_URL, api_url: str = GITHUB_API_URL) -> GitHubSetupFlow:
    """Parcours à partir des réglages (``settings.github_app_enabled`` doit être vrai)."""
    return GitHubSetupFlow(
        app_slug=settings.github_app_slug,
        public_url=settings.projects_public_url,
        states=SetupStates(repository, settings.github_app_client_secret, clock=clock),
        client=GitHubUserClient(settings.github_app_client_id, settings.github_app_client_secret, github_url=github_url, api_url=api_url),
        installations=repository,
    )
