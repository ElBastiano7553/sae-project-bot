"""Contrôle de santé (Docker ``HEALTHCHECK``, supervision) : ``python -m src.healthcheck``.

Lit le fichier de vie que le bot réécrit chaque minute (``data/heartbeat``).
Code de sortie 0 si le bot est connecté à Discord et que le fichier date de moins
de 3 minutes ; 1 sinon, avec la raison sur une ligne. Aucun secret n'est lu.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

from src.config import resolve_path

MAX_AGE = dt.timedelta(minutes=3)


def heartbeat_path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    db_path = resolve_path(env.get("DATABASE_PATH", "").strip() or "data/sae_bot.sqlite3")
    return db_path.parent / "heartbeat"


def check(path: Path, now: dt.datetime | None = None) -> tuple[bool, str]:
    now = now or dt.datetime.now(dt.UTC)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        at = dt.datetime.fromisoformat(data["at"])
    except FileNotFoundError:
        return False, f"pas de fichier de vie ({path}) : le bot n'a pas encore démarré"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "fichier de vie illisible"
    age = now - at
    if age > MAX_AGE:
        return False, f"dernier signe de vie il y a {int(age.total_seconds())} s : le bot est bloqué ou arrêté"
    if not data.get("ready"):
        return False, "le bot tourne mais n'est pas connecté à Discord"
    return True, f"ok (signe de vie il y a {int(age.total_seconds())} s)"


def main() -> int:
    healthy, message = check(heartbeat_path())
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
