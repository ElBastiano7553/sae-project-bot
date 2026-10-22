"""Sauvegardes de la base SQLite.

* La copie utilise l'API de sauvegarde de SQLite (``Connection.backup``) : elle est
  cohérente même si le bot écrit pendant la copie.
* Chaque sauvegarde est rouverte et vérifiée (``PRAGMA integrity_check``) ; une copie
  invalide est supprimée et l'erreur signalée.
* Rotation : seules les ``keep`` sauvegardes les plus récentes **créées par le bot**
  (nom ``sae_bot-AAAAMMJJ-HHMMSS[-raison].sqlite3``) sont conservées. Aucun autre
  fichier du dossier n'est jamais touché, et la base elle-même jamais modifiée.
* La restauration n'est pas automatique (elle remplacerait les données actuelles) :
  la procédure manuelle est décrite dans docs/deployment-linux.md.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

PREFIX = "sae_bot-"
_NAME = re.compile(r"^sae_bot-(\d{8}-\d{6})(?:-[a-z0-9]+)?\.sqlite3$")


class BackupError(Exception):
    """Sauvegarde impossible. Le message est destiné à l'utilisateur."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: dt.datetime
    size: int


def backup_name(now: dt.datetime, reason: str = "") -> str:
    suffix = f"-{re.sub(r'[^a-z0-9]', '', reason.lower())}" if reason else ""
    return f"{PREFIX}{now.astimezone(dt.UTC):%Y%m%d-%H%M%S}{suffix}.sqlite3"


def create_backup(
    conn: sqlite3.Connection, directory: Path, *, keep: int, reason: str = "", now: dt.datetime | None = None
) -> BackupInfo:
    """Copie la base dans ``directory``, vérifie la copie, puis applique la rotation."""
    if keep < 1:
        raise ValueError("keep doit valoir au moins 1")
    now = now or dt.datetime.now(dt.UTC)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"dossier de sauvegarde inaccessible ({directory}) : {exc.strerror}") from exc
    target = directory / backup_name(now, reason)
    counter = 1
    while target.exists():  # deux sauvegardes dans la même seconde
        target = directory / backup_name(now, f"{reason}{counter}")
        counter += 1
    partial = target.with_suffix(".partial")
    try:
        dest = sqlite3.connect(partial)
        try:
            conn.backup(dest)
            # La copie hérite du mode WAL de la base : on la repasse en fichier unique, sinon chaque
            # ouverture en lecture seule (vérification, restore-test) laisse des fichiers -wal/-shm orphelins.
            dest.execute("PRAGMA journal_mode = DELETE")
        finally:
            dest.close()
        verify_backup(partial)
        partial.replace(target)
    except (sqlite3.Error, OSError, BackupError) as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"sauvegarde impossible : {exc}") from exc
    finally:
        remove_sidecars(partial)
    prune_backups(directory, keep)
    return BackupInfo(target, now, target.stat().st_size)


def verify_backup(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise BackupError(f"copie corrompue ({result})")


def list_backups(directory: Path) -> list[BackupInfo]:
    """Sauvegardes créées par le bot, de la plus récente à la plus ancienne."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        match = _NAME.match(path.name)
        if match and path.is_file():
            created = dt.datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=dt.UTC)
            found.append(BackupInfo(path, created, path.stat().st_size))
    return sorted(found, key=lambda b: (b.created_at, b.path.name), reverse=True)


def prune_backups(directory: Path, keep: int) -> list[Path]:
    """Supprime les sauvegardes du bot au-delà des ``keep`` plus récentes. Retourne les fichiers supprimés."""
    removed = []
    for old in list_backups(directory)[keep:]:
        old.path.unlink(missing_ok=True)
        remove_sidecars(old.path)
        removed.append(old.path)
    return removed


def remove_sidecars(path: Path) -> None:
    """Fichiers -wal/-shm laissés à côté d'une copie (sauvegardes créées avant la version 1.2.1)."""
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
