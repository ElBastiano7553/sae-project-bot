"""Sauvegardes SQLite, contrôle de santé et sauvegarde avant migration."""

import datetime as dt
import json
import sqlite3

import pytest

from src.database import Database, KeyValueRepository
from src.healthcheck import check
from src.services.backup import BackupError, create_backup, list_backups, prune_backups

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)


def file_db(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    KeyValueRepository(db).set("cle", "valeur")
    return db


def test_backup_is_complete_and_verified(tmp_path):
    db = file_db(tmp_path)
    info = create_backup(db.conn, tmp_path / "backups", keep=3, now=NOW)
    db.close()
    assert info.path.name == "sae_bot-20261022-120000.sqlite3"
    copy = sqlite3.connect(info.path)
    assert copy.execute("SELECT value FROM kv_store WHERE key = 'cle'").fetchone() == ("valeur",)
    copy.close()
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_rotation_only_touches_bot_backups(tmp_path):
    db = file_db(tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "important.sqlite3").write_text("à ne pas toucher")
    for hour in range(5):
        create_backup(db.conn, folder, keep=3, now=NOW + dt.timedelta(hours=hour))
    db.close()
    names = [b.path.name for b in list_backups(folder)]
    assert names == ["sae_bot-20261022-160000.sqlite3", "sae_bot-20261022-150000.sqlite3", "sae_bot-20261022-140000.sqlite3"]
    assert (folder / "important.sqlite3").read_text() == "à ne pas toucher"
    assert prune_backups(folder, 3) == []


def test_two_backups_in_the_same_second(tmp_path):
    db = file_db(tmp_path)
    first = create_backup(db.conn, tmp_path / "b", keep=5, now=NOW)
    second = create_backup(db.conn, tmp_path / "b", keep=5, now=NOW)
    db.close()
    assert first.path != second.path and len(list_backups(tmp_path / "b")) == 2


def test_backup_to_unwritable_place_fails_cleanly(tmp_path):
    db = file_db(tmp_path)
    blocker = tmp_path / "fichier"
    blocker.write_text("x")
    with pytest.raises(BackupError, match="inaccessible"):
        create_backup(db.conn, blocker / "backups", keep=3)
    db.close()


def test_migration_of_existing_database_makes_a_backup_first(tmp_path):
    path = tmp_path / "ancienne.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE managed_messages (key TEXT PRIMARY KEY, channel_id INTEGER, message_id INTEGER, "
                       "content_hash TEXT, updated_at TEXT); PRAGMA user_version = 1;")
    conn.close()
    from src.database.connection import MIGRATIONS

    Database(path).close()
    backups = list_backups(tmp_path / "backups")
    assert len(backups) == 1 and backups[0].path.name.endswith(f"-avantv{len(MIGRATIONS)}.sqlite3")
    old = sqlite3.connect(backups[0].path)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1  # la copie est la base d'avant migration
    old.close()


def test_new_database_needs_no_backup(tmp_path):
    Database(tmp_path / "neuve.sqlite3").close()
    assert list_backups(tmp_path / "backups") == []


def test_failed_pre_migration_backup_leaves_database_untouched(tmp_path, monkeypatch):
    path = tmp_path / "ancienne.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE t (x); PRAGMA user_version = 1;")
    conn.close()

    def refuse(*args, **kwargs):
        raise BackupError("disque plein")

    monkeypatch.setattr("src.services.backup.create_backup", refuse)
    with pytest.raises(BackupError):
        Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_integrity_check(tmp_path):
    db = file_db(tmp_path)
    assert db.integrity_ok()
    db.close()


@pytest.mark.parametrize(
    "content, healthy, message",
    [
        ({"at": "2026-10-22T11:59:00+00:00", "ready": True}, True, "ok"),
        ({"at": "2026-10-22T11:50:00+00:00", "ready": True}, False, "bloqué"),
        ({"at": "2026-10-22T11:59:30+00:00", "ready": False}, False, "pas connecté"),
    ],
)
def test_healthcheck(tmp_path, content, healthy, message):
    path = tmp_path / "heartbeat"
    path.write_text(json.dumps(content))
    ok, text = check(path, NOW)
    assert ok is healthy and message in text


def test_healthcheck_without_file(tmp_path):
    ok, text = check(tmp_path / "absent", NOW)
    assert not ok and "pas encore démarré" in text
