"""Exploitation du serveur : instance unique, codes de sortie, outils src.ops, chien de garde systemd.

Ces tests utilisent des fichiers temporaires et de faux processus : ils ne prouvent pas le
comportement de systemd lui-même (à vérifier sur le VPS, voir docs/deployment-vps.md).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sqlite3
from collections import namedtuple

import discord
import pytest

from src import ops
from src.config import Settings
from src.database import Database
from src.instance_lock import AlreadyRunning, InstanceLock
from src.systemd_notify import notify

posix_only = pytest.mark.skipif(os.name != "posix", reason="verrou fcntl et sockets Unix : Linux uniquement")
Usage = namedtuple("Usage", "total used free")
NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def settings(tmp_path):
    return Settings.from_env(
        {"DISCORD_GUILD_ID": "42", "DATABASE_PATH": str(tmp_path / "data" / "sae_bot.sqlite3")}, require_token=False
    )


@pytest.fixture
def database(settings):
    db = Database(settings.database_path)
    with db.conn:
        db.conn.execute("INSERT INTO kv_store (key, value) VALUES ('marqueur', 'original')")
    yield db
    db.close()


def marker(path) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM kv_store WHERE key = 'marqueur'").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# --- Instance unique ---------------------------------------------------------------------------


@posix_only
def test_second_instance_is_refused_then_allowed_after_release(tmp_path):
    first, second = InstanceLock(tmp_path / "samae.lock"), InstanceLock(tmp_path / "samae.lock")
    assert first.acquire() is True
    with pytest.raises(AlreadyRunning, match=str(os.getpid())):
        second.acquire()
    first.release()
    assert second.acquire() is True  # libéré : un redémarrage n'est jamais bloqué
    second.release()


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "faux-token-de-test-0123456789")
    monkeypatch.setenv("DISCORD_GUILD_ID", "42")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "sae_bot.sqlite3"))
    monkeypatch.setattr("src.main.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("src.main.setup_logging", lambda *a, **k: None)
    return tmp_path / "data"


@posix_only
def test_main_refuses_to_start_a_second_instance(main_env, monkeypatch):
    from src import main as entry

    monkeypatch.setattr(entry.SaeBot, "run", lambda *a, **k: pytest.fail("le bot ne doit pas se connecter"))
    held = InstanceLock(main_env / "samae.lock")
    held.acquire()
    try:
        assert entry.main() == entry.EXIT_ALREADY_RUNNING == 3
    finally:
        held.release()


@pytest.mark.parametrize(
    ("error", "code"),
    [(discord.LoginFailure("token refusé"), 2), (RuntimeError("bug"), 1), (None, 0)],
)
def test_main_exit_codes_drive_systemd_restarts(main_env, monkeypatch, error, code):
    from src import main as entry

    def fake_run(self, *args, **kwargs):
        if error is not None:
            raise error

    monkeypatch.setattr(entry.SaeBot, "run", fake_run)
    assert entry.main() == code
    if os.name == "posix":  # le verrou est rendu à la sortie
        lock = InstanceLock(main_env / "samae.lock")
        assert lock.acquire() is True
        lock.release()


# --- Chien de garde systemd -------------------------------------------------------------------------


@posix_only
def test_watchdog_notification_reaches_systemd_socket(tmp_path):
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        assert notify("WATCHDOG=1", {"NOTIFY_SOCKET": path}) is True
        assert server.recv(64) == b"WATCHDOG=1"


def test_watchdog_is_silent_outside_systemd(tmp_path):
    assert notify("WATCHDOG=1", {}) is False
    assert notify("WATCHDOG=1", {"NOTIFY_SOCKET": str(tmp_path / "absent.sock")}) is False


# --- Sauvegardes et restauration -----------------------------------------------------------------


def test_backup_verify_and_restore_test_never_touch_the_live_database(settings, database, capsys):
    assert ops.cmd_backup(settings) == 0
    backups = list((settings.backup_dir / ops.DEPLOY_SUBDIR).glob("*.sqlite3"))
    assert len(backups) == 1 and "predeploiement" in backups[0].name
    assert ops.cmd_verify(settings, None) == 0
    assert ops.cmd_restore_test(settings, str(backups[0])) == 0
    out = capsys.readouterr().out
    assert "intégrité OK" in out and "est restaurable" in out and "n'a pas été modifiée" in out
    assert marker(settings.database_path) == "original"


def test_corrupted_backup_is_reported(settings, database, tmp_path, capsys):
    broken = tmp_path / "sae_bot-20261022-120000.sqlite3"
    broken.write_bytes(b"pas une base sqlite" * 100)
    assert ops.cmd_verify(settings, str(broken)) == 1
    assert ops.cmd_restore_test(settings, str(broken)) == 1
    assert "invalide" in capsys.readouterr().out


def test_restore_needs_confirmation_and_keeps_the_old_database(settings, database, capsys):
    assert ops.cmd_backup(settings) == 0
    backup = next((settings.backup_dir / ops.DEPLOY_SUBDIR).glob("*.sqlite3"))
    with database.conn:
        database.conn.execute("UPDATE kv_store SET value = 'modifié' WHERE key = 'marqueur'")
    database.close()

    assert ops.cmd_restore(settings, str(backup), confirm=False) == 1
    assert marker(settings.database_path) == "modifié"  # simulation : rien n'a bougé
    assert "Simulation" in capsys.readouterr().out

    assert ops.cmd_restore(settings, str(backup), confirm=True, now=NOW) == 0
    assert marker(settings.database_path) == "original"
    aside = settings.database_path.parent / "avant-restauration-20261022-120000"
    assert marker(aside / settings.database_path.name) == "modifié"  # déplacée, jamais supprimée


@posix_only
def test_restore_is_refused_while_the_bot_runs(settings, database, capsys):
    assert ops.cmd_backup(settings) == 0
    backup = next((settings.backup_dir / ops.DEPLOY_SUBDIR).glob("*.sqlite3"))
    running = InstanceLock(settings.database_path.parent / "samae.lock")
    running.acquire()
    try:
        assert ops.cmd_restore(settings, str(backup), confirm=True) == 1
    finally:
        running.release()
    assert "refusée" in capsys.readouterr().out
    assert not list(settings.database_path.parent.glob("avant-restauration-*"))


def test_resync_forgets_the_command_fingerprint(settings, database):
    from src.discord_bot.bot import COMMANDS_SYNC_GLOBAL_KEY, COMMANDS_SYNC_HOME_KEY

    keys = (COMMANDS_SYNC_GLOBAL_KEY, COMMANDS_SYNC_HOME_KEY, "commands_sync_hash")  # la dernière : avant 1.12
    with database.conn:
        for key in keys:
            database.conn.execute("INSERT INTO kv_store (key, value) VALUES (?, 'abc')", (key,))
        database.conn.execute("INSERT INTO kv_store (key, value) VALUES ('setup_confirmed_at', 'x')")
    assert ops.cmd_resync(settings) == 0
    for key in keys:
        assert database.conn.execute("SELECT 1 FROM kv_store WHERE key = ?", (key,)).fetchone() is None
    assert database.conn.execute("SELECT 1 FROM kv_store WHERE key = 'setup_confirmed_at'").fetchone()  # rien d'autre


# --- Surveillance ----------------------------------------------------------------------------------


def write_heartbeat(settings, *, at=NOW, ready=True, pid=4242):
    settings.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    settings.heartbeat_path.write_text(json.dumps({"at": at.isoformat(), "ready": ready, "pid": pid}))


def fake_proc(tmp_path, rss_kb: int):
    (tmp_path / "proc" / "4242").mkdir(parents=True)
    (tmp_path / "proc" / "4242" / "status").write_text(f"Name:\tpython\nVmRSS:\t  {rss_kb} kB\n")
    return tmp_path / "proc"


def test_check_reports_a_healthy_bot(settings, database, tmp_path, monkeypatch):
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda _: Usage(100 * 2**30, 40 * 2**30, 60 * 2**30))
    write_heartbeat(settings)
    ops.cmd_backup(settings)  # dans deploiements/ : ne compte pas comme sauvegarde automatique
    from src.services.backup import create_backup

    create_backup(database.conn, settings.backup_dir, keep=3, now=NOW - dt.timedelta(hours=3))
    report = ops.collect_report(settings, now=NOW + dt.timedelta(seconds=30), proc=fake_proc(tmp_path, 120_000))
    assert report.problems == {}
    text = " ".join(report.infos)
    assert "mémoire du bot : 117 Mo" in text and "40 % utilisé" in text and "il y a 3 h" in text


def test_check_detects_a_stopped_bot_full_disk_and_old_backups(settings, database, tmp_path, monkeypatch):
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda _: Usage(100 * 2**30, 95 * 2**30, 5 * 2**30))
    write_heartbeat(settings, at=NOW - dt.timedelta(minutes=10))
    report = ops.collect_report(settings, now=NOW, proc=fake_proc(tmp_path, 600_000))
    assert set(report.problems) == {"bot", "memoire", "disque", "sauvegarde"}


def test_alerts_are_sent_only_when_the_state_changes():
    report = ops.Report(problems={"bot": "bot : arrêté"})
    first, state = ops.alert_messages(report, {}, NOW)
    assert len(first) == 1 and "problème détecté" in first[0]
    again, state = ops.alert_messages(report, state, NOW + dt.timedelta(minutes=5))
    assert again == []  # pas de spam toutes les 5 minutes
    reminder, state = ops.alert_messages(report, state, NOW + dt.timedelta(hours=13))
    assert len(reminder) == 1 and "toujours présent" in reminder[0]
    back, state = ops.alert_messages(ops.Report(), state, NOW + dt.timedelta(hours=14))
    assert back == ["✅ **Samae : tout est revenu à la normale.**"] and state["problems"] == []


def test_check_sends_alert_through_webhook_and_retries_after_failure(settings, database, monkeypatch):
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda _: Usage(100, 10, 90 * 2**30))
    sent = []
    env = {"ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"}
    assert ops.cmd_check(settings, alert=True, env=env, now=NOW, sender=lambda url, text: False) == 1
    assert ops.cmd_check(settings, alert=True, env=env, now=NOW, sender=lambda url, text: sent.append(text) or True) == 1
    assert len(sent) == 1 and "pas de fichier de vie" in sent[0]  # l'échec précédent n'a pas été oublié


def test_check_refuses_a_non_discord_webhook_url(settings, database, monkeypatch, capsys):
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda _: Usage(100, 10, 90 * 2**30))
    called = []
    ops.cmd_check(settings, alert=True, env={"ALERT_WEBHOOK_URL": "http://exemple.org/hook"}, now=NOW,
                  sender=lambda *a: called.append(a) or True)
    assert called == [] and "n'est pas une adresse de webhook Discord" in capsys.readouterr().out


def test_backups_are_single_files_without_wal_leftovers(settings, database):
    """Une copie en mode WAL laissait des -wal/-shm orphelins à chaque vérification."""
    assert ops.cmd_backup(settings) == 0
    directory = settings.backup_dir / ops.DEPLOY_SUBDIR
    assert ops.cmd_verify(settings, None) == 0 and ops.cmd_restore_test(settings, None) == 0
    names = sorted(p.name for p in directory.iterdir())
    assert len(names) == 1 and names[0].endswith(".sqlite3"), names
    conn = sqlite3.connect(directory / names[0])
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_pruning_also_removes_old_sidecar_files(tmp_path):
    from src.services.backup import prune_backups

    for stamp in ("20261020-120000", "20261021-120000"):
        (tmp_path / f"sae_bot-{stamp}.sqlite3").write_bytes(b"x")
        (tmp_path / f"sae_bot-{stamp}.sqlite3-shm").write_bytes(b"x")
    prune_backups(tmp_path, keep=1)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sae_bot-20261021-120000.sqlite3", "sae_bot-20261021-120000.sqlite3-shm"]
