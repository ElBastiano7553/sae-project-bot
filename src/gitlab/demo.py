"""Envoie un faux webhook GitLab au bot pour tester la V2 sans toucher à GitLab.

Le bot doit être lancé dans un autre terminal (``python -m src.main``). Exemples :

    python -m src.gitlab.demo push
    python -m src.gitlab.demo merge_request
    python -m src.gitlab.demo pipeline
    python -m src.gitlab.demo push --wrong-secret      # doit être refusé (401)
    python -m src.gitlab.demo push --url https://xxx.trycloudflare.com/webhooks/gitlab

Le secret est lu dans ``.env`` et n'est jamais affiché.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from urllib.parse import urlsplit

import aiohttp
from dotenv import load_dotenv

from src.config import PROJECT_ROOT, ConfigError, Settings
from src.gitlab.samples import deployment_payload, merge_request_payload, pipeline_payload, push_payload
from src.gitlab.webhook import WEBHOOK_PATH

EVENTS = {
    "push": ("Push Hook", push_payload),
    "merge_request": ("Merge Request Hook", merge_request_payload),
    "pipeline": ("Pipeline Hook", pipeline_payload),
    "deployment": ("Deployment Hook", deployment_payload),
}


async def send(url: str, event: str, secret: str, payload: dict) -> tuple[int, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Gitlab-Event": event,
        "X-Gitlab-Token": secret,
        "X-Gitlab-Event-UUID": str(uuid.uuid4()),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=json.dumps(payload), headers=headers) as response:
            return response.status, await response.text()


def is_safe_target(url: str) -> bool:
    """Le secret ne part en clair (http://) que vers ce PC lui-même."""
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Envoie un faux webhook GitLab au bot.")
    parser.add_argument("event", choices=sorted(EVENTS))
    parser.add_argument("--wrong-secret", action="store_true", help="utilise un faux secret (le bot doit répondre 401)")
    parser.add_argument("--url", help="adresse complète du webhook (par défaut : le serveur local du .env)")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        settings = Settings.from_env(require_token=False)
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    if not settings.gitlab_webhooks_enabled:
        print("❌ GITLAB_WEBHOOK_SECRET est vide dans .env : la V2 est désactivée.", file=sys.stderr)
        return 2

    host = "127.0.0.1" if settings.webhook_host in {"0.0.0.0", "::"} else settings.webhook_host
    url = args.url or f"http://{host}:{settings.webhook_port}{WEBHOOK_PATH}"
    if not is_safe_target(url):
        print("❌ Pour une adresse distante, utilisez https:// : le secret ne doit jamais circuler en clair.", file=sys.stderr)
        return 2
    event, build = EVENTS[args.event]
    payload = build()
    if settings.gitlab_project_id is not None:
        payload["project"]["id"] = settings.gitlab_project_id
    secret = "faux-secret-de-demonstration" if args.wrong_secret else settings.gitlab_webhook_secret

    try:
        status, body = asyncio.run(send(url, event, secret, payload))
    except aiohttp.ClientError as exc:
        print(f"❌ Impossible de joindre {url} ({exc}). Le bot est-il lancé ?", file=sys.stderr)
        return 1
    print(f"{url} → HTTP {status} {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
