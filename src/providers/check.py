"""Contrôle de bout en bout de la réception des projets GitHub (étapes B3 et B4), sur le serveur lui-même.

Envoie de faux événements de la GitHub App, signés avec ``GITHUB_APP_WEBHOOK_SECRET``, à la
réception locale de Samae (``http://127.0.0.1:<PROJECTS_HTTP_PORT>/github/webhook``), puis
compare chaque réponse à celle attendue. Le bot doit tourner, et le dépôt indiqué doit déjà être
connecté avec ``/projet configurer`` (salon « Code » choisi avec ``/projet modifier``) :

    python -m src.providers.check --depot equipe/depot-de-test --installation 12345678

Scénarios : événement valide (une notification « Code » est publiée dans le salon du projet),
même livraison renvoyée, même fait sous une autre livraison, signature fausse, signature
absente, signature faite avec l'ancien secret ``GITHUB_WEBHOOK_SECRET``, autre installation,
dépôt non relié, et une branche écartée si un filtre de branches est réglé.

Le secret est lu dans ``.env`` et n'est jamais affiché ni envoyé : seule la signature HMAC du
corps part dans la requête. La cible est toujours ce serveur (adresse locale), jamais Internet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import uuid
from dataclasses import dataclass

import aiohttp
from dotenv import load_dotenv

from src.config import PROJECT_ROOT, ConfigError, Settings
from src.github.samples import push_payload
from src.github.security import SIGNATURE_HEADER, compute_signature
from src.providers.server import GITHUB_PATH

FILTER_TEST_BRANCH = "samae-controle-filtre"


@dataclass(frozen=True)
class Step:
    name: str
    expected: tuple[tuple[int, str], ...]  # (code HTTP, statut) acceptés
    status: int = 0
    answer: str = ""

    @property
    def ok(self) -> bool:
        return (self.status, self.answer) in self.expected


def make_push(repository: str, installation_id: int, *, branch: str = "main") -> dict:
    payload = push_payload()
    sha = secrets.token_hex(20)  # nouveau commit fictif à chaque contrôle : pas de doublon d'un contrôle à l'autre
    payload["after"] = sha
    payload["ref"] = f"refs/heads/{branch}"
    payload["commits"][0]["id"] = sha
    payload["commits"][0]["message"] = "Contrôle de bout en bout de Samae (message de test, aucune action nécessaire)"
    payload["repository"] = {**payload["repository"], "full_name": repository, "name": repository.split("/")[-1]}
    payload["installation"] = {"id": installation_id}
    return payload


async def post(session: aiohttp.ClientSession, url: str, payload: dict, *, secret: str | None, delivery: str) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "push", "X-GitHub-Delivery": delivery}
    if secret is not None:
        headers[SIGNATURE_HEADER] = compute_signature(secret, body)
    async with session.post(url, data=body, headers=headers) as response:
        try:
            answer = (await response.json(content_type=None)).get("status", "")
        except (ValueError, AttributeError):
            answer = ""
        return response.status, answer


async def run_checks(url: str, secret: str, repository: str, installation_id: int, *, legacy_secret: str = "", filtered: bool) -> list[Step]:
    steps: list[Step] = []
    valid = make_push(repository, installation_id)
    delivery = str(uuid.uuid4())
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:

        async def check(name, payload, expected, *, key=secret, delivery_id=None):
            status, answer = await post(session, url, payload, secret=key, delivery=delivery_id or str(uuid.uuid4()))
            steps.append(Step(name, expected, status, answer))

        await check("événement valide → notification publiée", valid, ((200, "queued"),), delivery_id=delivery)
        await check("même livraison renvoyée → doublon", valid, ((200, "duplicate"),), delivery_id=delivery)
        await check("même fait, autre livraison → doublon", valid, ((200, "duplicate"),))
        await check("signature fausse → refusé", make_push(repository, installation_id), ((401, "unauthorized"),), key="faux-secret-de-controle-0000")
        await check("signature absente → refusé", make_push(repository, installation_id), ((401, "unauthorized"),), key=None)
        if legacy_secret:
            await check("signé avec l'ancien secret → refusé", make_push(repository, installation_id), ((401, "unauthorized"),), key=legacy_secret)
        await check("autre installation → ignoré", make_push(repository, installation_id + 1), ((200, "ignored"),))
        await check("dépôt non relié → ignoré", make_push("samae-controle/depot-inconnu", installation_id), ((200, "ignored"),))
        if filtered:
            await check(
                f"branche « {FILTER_TEST_BRANCH} » → écartée par le filtre",
                make_push(repository, installation_id, branch=FILTER_TEST_BRANCH),
                ((200, "recorded"),),
            )
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contrôle de bout en bout de la réception des projets GitHub (local).")
    parser.add_argument("--depot", required=True, help="dépôt connecté avec /projet configurer, par exemple equipe/depot-de-test")
    parser.add_argument("--installation", required=True, type=int, help="numéro de l'installation de la GitHub App (visible dans son adresse sur GitHub)")
    parser.add_argument(
        "--filtre", action="store_true",
        help=f"vérifie aussi qu'une branche non suivie est écartée (le salon « Code » doit avoir un filtre de branches, sans « {FILTER_TEST_BRANCH} »)",
    )
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        settings = Settings.from_env(require_token=False)
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    if not settings.projects_http_enabled or not settings.github_app_webhook_secret:
        print("❌ Réception des projets inactive ou GITHUB_APP_WEBHOOK_SECRET vide dans .env.", file=sys.stderr)
        return 2
    host = settings.projects_http_host
    host = f"[{host}]" if ":" in host else host
    url = f"http://{host}:{settings.projects_http_port}{GITHUB_PATH}"
    try:
        steps = asyncio.run(
            run_checks(
                url, settings.github_app_webhook_secret, args.depot, args.installation,
                legacy_secret=settings.github_webhook_secret, filtered=args.filtre,
            )
        )
    except (aiohttp.ClientError, TimeoutError) as exc:
        print(f"❌ Impossible de joindre {url} ({type(exc).__name__}). Le bot est-il lancé ?", file=sys.stderr)
        return 1
    for step in steps:
        expected = " ou ".join(f"{code} {status}" for code, status in step.expected)
        mark = "✅" if step.ok else "❌"
        print(f"{mark} {step.name} : HTTP {step.status} {step.answer or '?'} (attendu : {expected})")
    failed = [s for s in steps if not s.ok]
    if failed:
        print(f"\n{len(failed)} contrôle(s) en échec : voir docs/github-app.md, section « Contrôle de bout en bout ».")
        return 1
    print("\nTous les contrôles sont conformes. Vérifiez dans Discord qu'UN seul message « Contrôle de bout en bout » est arrivé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
