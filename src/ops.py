"""Outils d'exploitation du serveur : ``python -m src.ops <commande>``.

Commandes (à lancer depuis le dossier du projet, environnement virtuel activé) :

* ``backup``                  sauvegarde vérifiée maintenant, bot en marche ou non
                              (rangée dans ``<BACKUP_DIR>/deploiements``, 10 conservées) ;
* ``verify [FICHIER]``        vérifie une sauvegarde (la plus récente par défaut) ;
* ``restore-test [FICHIER]``  prouve qu'une sauvegarde est restaurable : elle est copiée
                              dans un dossier temporaire et ouverte par le code actuel ;
                              la vraie base n'est jamais touchée ;
* ``restore FICHIER --confirm``  remplace la base par une sauvegarde. Refusé tant que le
                              bot tourne. L'ancienne base est **déplacée**, jamais supprimée ;
* ``check [--alert]``         état du bot, mémoire, disque, sauvegardes. Avec ``--alert`` et
                              ``ALERT_WEBHOOK_URL``, prévient sur Discord quand l'état change ;
* ``resync``                  force la synchronisation des commandes slash au prochain démarrage.

Codes de sortie : 0 = tout va bien, 1 = problème constaté, 2 = configuration ou usage incorrect.
Aucun secret n'est lu en dehors de ``.env`` ni affiché.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from src.config import PROJECT_ROOT, ConfigError, Settings
from src.healthcheck import check as check_heartbeat
from src.instance_lock import LOCK_NAME, AlreadyRunning, InstanceLock
from src.services.backup import BackupError, create_backup, list_backups, remove_sidecars, verify_backup

DEPLOY_SUBDIR = "deploiements"
DEPLOY_KEEP = 10
STATE_FILE = "surveillance.json"
# Seuils de la surveillance (VPS modeste ; le bot consomme d'ordinaire 80 à 150 Mo).
MEMORY_WARN_MB = 400
DISK_WARN_PERCENT = 90
DISK_MIN_FREE_MB = 500
# Un problème qui dure est rappelé, sans plus (pas de spam) :
REMINDER_HOURS = 12
WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
EXIT_OK, EXIT_PROBLEM, EXIT_USAGE = 0, 1, 2


def _load_settings(env: dict | None = None) -> Settings:
    if env is None:
        load_dotenv(PROJECT_ROOT / ".env")
    return Settings.from_env(env, require_token=False)


def _out(text: str = "") -> None:
    print(text, flush=True)


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    wanted = ("meetings", "gitlab_events", "managed_messages", "channel_bindings")
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in wanted if name in names}


def _schema(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _pick_backup(settings: Settings, raw: str | None) -> Path:
    if raw:
        path = Path(raw)
        if not path.is_file():
            raise BackupError(f"fichier introuvable : {path}")
        return path
    candidates = list_backups(settings.backup_dir) + list_backups(settings.backup_dir / DEPLOY_SUBDIR)
    if not candidates:
        raise BackupError(f"aucune sauvegarde dans {settings.backup_dir}")
    return max(candidates, key=lambda b: b.created_at).path


# --- Sauvegardes -------------------------------------------------------------------------


def cmd_backup(settings: Settings, reason: str = "predeploiement") -> int:
    db = settings.database_path
    if not db.is_file():
        _out(f"❌ Base introuvable : {db}")
        return EXIT_USAGE
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        info = create_backup(conn, settings.backup_dir / DEPLOY_SUBDIR, keep=DEPLOY_KEEP, reason=reason)
        schema = _schema(conn)
    except BackupError as exc:
        _out(f"❌ {exc}")
        return EXIT_PROBLEM
    finally:
        conn.close()
    _out(f"✅ Sauvegarde vérifiée : {info.path} ({info.size} octets, schéma v{schema})")
    return EXIT_OK


def cmd_verify(settings: Settings, raw: str | None) -> int:
    try:
        path = _pick_backup(settings, raw)
        verify_backup(path)
    except (BackupError, sqlite3.Error) as exc:
        _out(f"❌ Sauvegarde invalide : {exc}")
        return EXIT_PROBLEM
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        schema, counts = _schema(conn), _table_counts(conn)
    finally:
        conn.close()
    _out(f"✅ {path.name} : intégrité OK, schéma v{schema}, " + ", ".join(f"{k} = {v}" for k, v in counts.items()))
    return EXIT_OK


def cmd_restore_test(settings: Settings, raw: str | None) -> int:
    """Restauration « à blanc » : la copie temporaire est ouverte comme le ferait le bot."""
    from src.database import Database  # import local : la base n'est pas nécessaire aux autres commandes

    try:
        source = _pick_backup(settings, raw)
        verify_backup(source)
    except (BackupError, sqlite3.Error) as exc:
        _out(f"❌ Sauvegarde invalide : {exc}")
        return EXIT_PROBLEM
    with tempfile.TemporaryDirectory(prefix="samae-restore-test-") as tmp:
        copy = Path(tmp) / "sae_bot.sqlite3"
        shutil.copy2(source, copy)
        try:
            database = Database(copy, backup_dir=Path(tmp) / "backups")
        except (RuntimeError, sqlite3.Error, BackupError) as exc:
            _out(f"❌ {source.name} ne peut pas être ouverte par cette version du bot : {exc}")
            return EXIT_PROBLEM
        try:
            before = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            try:
                original_schema = _schema(before)
            finally:
                before.close()
            healthy = database.integrity_ok()
            schema, counts = database.schema_version, _table_counts(database.conn)
        finally:
            database.close()
    if not healthy:
        _out(f"❌ {source.name} : la copie restaurée échoue à la vérification d'intégrité")
        return EXIT_PROBLEM
    migrated = f" (migrée de v{original_schema} à v{schema} dans la copie)" if schema != original_schema else ""
    _out(f"✅ {source.name} est restaurable : schéma v{schema}{migrated}, " + ", ".join(f"{k} = {v}" for k, v in counts.items()))
    _out("   La vraie base n'a pas été modifiée.")
    return EXIT_OK


def cmd_restore(settings: Settings, raw: str, *, confirm: bool, now: dt.datetime | None = None) -> int:
    db = settings.database_path
    try:
        source = _pick_backup(settings, raw)
        verify_backup(source)
    except (BackupError, sqlite3.Error) as exc:
        _out(f"❌ Sauvegarde invalide, rien n'a été modifié : {exc}")
        return EXIT_PROBLEM
    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%d-%H%M%S")
    aside = db.parent / f"avant-restauration-{stamp}"
    if not confirm:
        _out("Simulation (rien n'a été modifié). Avec --confirm :")
        _out(f"  1. la base actuelle {db.name} (et ses fichiers -wal/-shm) serait déplacée dans {aside}/")
        _out(f"  2. {source} serait copiée à sa place, puis vérifiée")
        _out("Le bot doit être arrêté avant : sudo systemctl stop samae")
        return EXIT_PROBLEM
    lock = InstanceLock(db.parent / LOCK_NAME)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        _out(f"❌ Restauration refusée, rien n'a été modifié : {exc}")
        return EXIT_PROBLEM
    try:
        aside.mkdir(parents=True, exist_ok=False)
        moved = []
        for suffix in ("", "-wal", "-shm"):
            current = Path(f"{db}{suffix}")
            if current.exists():
                current.replace(aside / current.name)
                moved.append(current.name)
        partial = db.with_name(db.name + ".restauration")
        shutil.copy2(source, partial)
        verify_backup(partial)
        remove_sidecars(partial)  # copie d'une ancienne sauvegarde en mode WAL
        partial.replace(db)
    except (OSError, BackupError, sqlite3.Error) as exc:
        _out(f"❌ Restauration interrompue : {exc}. L'ancienne base est dans {aside}/ : remettez-la à sa place.")
        return EXIT_PROBLEM
    finally:
        lock.release()
    _out(f"✅ Base restaurée depuis {source.name}. Ancienne base conservée dans {aside}/ ({', '.join(moved) or 'aucun fichier'}).")
    _out("   Redémarrez le bot : sudo systemctl start samae")
    return EXIT_OK


# --- Surveillance --------------------------------------------------------------------------


@dataclass
class Report:
    problems: dict[str, str] = field(default_factory=dict)  # clé stable → description
    infos: list[str] = field(default_factory=list)


def _process_memory_mb(pid: int, proc: Path = Path("/proc")) -> float | None:
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def collect_report(settings: Settings, *, now: dt.datetime | None = None, proc: Path = Path("/proc")) -> Report:
    now = now or dt.datetime.now(dt.UTC)
    report = Report()
    heartbeat = settings.heartbeat_path
    healthy, message = check_heartbeat(heartbeat, now)
    if healthy:
        report.infos.append(f"bot : {message}")
    else:
        report.problems["bot"] = f"bot : {message}"

    pid = None
    try:
        pid = int(json.loads(heartbeat.read_text(encoding="utf-8")).get("pid"))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    memory = _process_memory_mb(pid, proc) if pid else None
    if memory is not None:
        line = f"mémoire du bot : {memory:.0f} Mo"
        if memory > MEMORY_WARN_MB:
            report.problems["memoire"] = f"{line} (seuil {MEMORY_WARN_MB} Mo)"
        else:
            report.infos.append(line)

    try:
        usage = shutil.disk_usage(settings.database_path.parent)
    except OSError:
        usage = None
    if usage is not None:
        percent = usage.used * 100 / usage.total
        free_mb = usage.free / 1024 / 1024
        line = f"disque : {percent:.0f} % utilisé, {free_mb:.0f} Mo libres"
        if percent > DISK_WARN_PERCENT or free_mb < DISK_MIN_FREE_MB:
            report.problems["disque"] = line
        else:
            report.infos.append(line)

    backups = list_backups(settings.backup_dir)
    if backups:
        age_h = (now - backups[0].created_at).total_seconds() / 3600
        line = f"dernière sauvegarde automatique il y a {age_h:.0f} h ({len(backups)} conservées)"
        if settings.backup_interval_hours and age_h > 2 * settings.backup_interval_hours:
            report.problems["sauvegarde"] = line + " : en retard"
        else:
            report.infos.append(line)
    elif settings.backup_interval_hours:
        report.problems["sauvegarde"] = "aucune sauvegarde automatique trouvée"

    try:
        load1, _, _ = os.getloadavg()
        report.infos.append(f"charge CPU (1 min) : {load1:.2f}")
    except (OSError, AttributeError):
        pass
    return report


def alert_messages(report: Report, state: dict, now: dt.datetime) -> tuple[list[str], dict]:
    """Messages à envoyer et nouvel état. Alerte seulement quand l'état change, ou en rappel espacé."""
    previous = set(state.get("problems", []))
    current = set(report.problems)
    last = state.get("alerted_at")
    last_at = dt.datetime.fromisoformat(last) if last else None
    messages: list[str] = []
    new = current - previous
    if new:
        messages.append("⚠️ **Samae : problème détecté**\n" + "\n".join(f"• {report.problems[k]}" for k in sorted(new)))
    elif current and (last_at is None or now - last_at >= dt.timedelta(hours=REMINDER_HOURS)):
        messages.append("⚠️ **Samae : problème toujours présent**\n" + "\n".join(f"• {report.problems[k]}" for k in sorted(current)))
    resolved = previous - current
    if resolved and not current:
        messages.append("✅ **Samae : tout est revenu à la normale.**")
    new_state = {"problems": sorted(current), "alerted_at": now.isoformat(timespec="seconds") if messages else last}
    return messages, new_state


def send_webhook(url: str, content: str, *, timeout: float = 10) -> bool:
    """Envoie un message par webhook Discord (sans aucune mention). Le contenu ne contient jamais de secret."""
    body = json.dumps({"content": content[:1900], "allowed_mentions": {"parse": []}}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "Samae-ops"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False


def cmd_check(settings: Settings, *, alert: bool, env: dict | None = None, now: dt.datetime | None = None, sender=send_webhook) -> int:
    now = now or dt.datetime.now(dt.UTC)
    report = collect_report(settings, now=now)
    for line in report.infos:
        _out(f"✅ {line}")
    for line in report.problems.values():
        _out(f"❌ {line}")
    if alert:
        env = os.environ if env is None else env
        url = env.get("ALERT_WEBHOOK_URL", "").strip()
        state_path = settings.database_path.parent / STATE_FILE
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        messages, new_state = alert_messages(report, state, now)
        if messages and not url:
            _out("ℹ️ ALERT_WEBHOOK_URL vide : alerte visible seulement dans les journaux (journalctl -u samae-check).")
        elif messages and not url.startswith(WEBHOOK_PREFIXES):
            _out("⚠️ ALERT_WEBHOOK_URL n'est pas une adresse de webhook Discord : alerte non envoyée.")
        else:
            for message in messages:
                if not sender(url, message):
                    _out("⚠️ Alerte Discord non envoyée (réseau ou webhook supprimé) : nouvel essai au prochain contrôle.")
                    new_state = state  # rien n'est marqué comme envoyé
                    break
        try:
            state_path.write_text(json.dumps(new_state), encoding="utf-8")
        except OSError as exc:
            _out(f"⚠️ État de la surveillance non enregistré : {exc.strerror}")
    return EXIT_PROBLEM if report.problems else EXIT_OK


def cmd_resync(settings: Settings) -> int:
    from src.discord_bot.bot import COMMANDS_SYNC_KEY  # import local : discord.py n'est utile qu'ici

    conn = sqlite3.connect(settings.database_path)
    try:
        with conn:
            conn.execute("DELETE FROM kv_store WHERE key = ?", (COMMANDS_SYNC_KEY,))
    except sqlite3.Error as exc:
        _out(f"❌ {exc}")
        return EXIT_PROBLEM
    finally:
        conn.close()
    _out("✅ Les commandes slash seront resynchronisées au prochain démarrage : sudo systemctl restart samae")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.ops", description="Outils d'exploitation de Samae")
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="sauvegarde vérifiée maintenant")
    backup.add_argument("--raison", default="predeploiement")
    sub.add_parser("verify", help="vérifier une sauvegarde").add_argument("fichier", nargs="?")
    sub.add_parser("restore-test", help="tester la restauration d'une sauvegarde").add_argument("fichier", nargs="?")
    restore = sub.add_parser("restore", help="restaurer une sauvegarde (bot arrêté)")
    restore.add_argument("fichier")
    restore.add_argument("--confirm", action="store_true")
    sub.add_parser("check", help="état du bot et du serveur").add_argument("--alert", action="store_true")
    sub.add_parser("resync", help="resynchroniser les commandes slash au prochain démarrage")
    args = parser.parse_args(argv)
    try:
        settings = _load_settings()
    except ConfigError as exc:
        _out(f"❌ Configuration invalide (.env) : {exc}")
        return EXIT_USAGE
    if args.command == "backup":
        return cmd_backup(settings, args.raison)
    if args.command == "verify":
        return cmd_verify(settings, args.fichier)
    if args.command == "restore-test":
        return cmd_restore_test(settings, args.fichier)
    if args.command == "restore":
        return cmd_restore(settings, args.fichier, confirm=args.confirm)
    if args.command == "check":
        return cmd_check(settings, alert=args.alert)
    return cmd_resync(settings)


if __name__ == "__main__":
    sys.exit(main())
