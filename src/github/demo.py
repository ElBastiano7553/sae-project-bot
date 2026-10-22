"""Envoie un faux webhook GitHub signé au bot, pour tester sans toucher à GitHub.

Le bot doit être lancé dans un autre terminal (``python -m src.main``). Exemples :

    python -m src.github.demo ping
    python -m src.github.demo push
    python -m src.github.demo pull_request
    python -m src.github.demo issues
    python -m src.github.demo workflow_run
    python -m src.github.demo push --wrong-secret      # doit être refusé (401)

Le secret est lu dans ``.env`` et n'est jamais affiché ni envoyé : seule la
signature HMAC du corps part dans la requête. La cible est toujours ce PC.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

import aiohttp
from dotenv import load_dotenv

from src.config import PROJECT_ROOT, ConfigError, Settings
from src.github.samples import issue_payload, ping_payload, pull_request_payload, push_payload, workflow_run_payload
from src.github.security import SIGNATURE_HEADER, compute_signature
from src.github.webhook import WEBHOOK_PATH

EVENTS = {
    "ping": ping_payload,
    "push": push_payload,
    "pull_request": pull_request_payload,
    "issues": issue_payload,
    "workflow_run": workflow_run_payload,
}


async def send(url: str, event: str, secret: str, payload: dict) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(uuid.uuid4()),
        SIGNATURE_HEADER: compute_signature(secret, body),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=body, headers=headers) as response:
            return response.status, await response.text()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Envoie un faux webhook GitHub signé au bot local.")
    parser.add_argument("event", choices=sorted(EVENTS))
    parser.add_argument("--wrong-secret", action="store_true", help="signe avec un faux secret (le bot doit répondre 401)")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        settings = Settings.from_env(require_token=False)
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    if not settings.github_webhooks_enabled:
        print("❌ GITHUB_WEBHOOK_SECRET est vide ou invalide dans .env : GitHub est désactivé.", file=sys.stderr)
        return 2

    host = "127.0.0.1" if settings.github_webhook_host in {"0.0.0.0", "::"} else settings.github_webhook_host
    url = f"http://{host}:{settings.github_webhook_port}{WEBHOOK_PATH}"
    payload = EVENTS[args.event]()
    payload["repository"]["full_name"] = settings.github_repository
    secret = "faux-secret-de-demonstration" if args.wrong_secret else settings.github_webhook_secret

    try:
        status, body = asyncio.run(send(url, args.event, secret, payload))
    except aiohttp.ClientError as exc:
        print(f"❌ Impossible de joindre {url} ({exc}). Le bot est-il lancé ?", file=sys.stderr)
        return 1
    print(f"{url} → HTTP {status} {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
