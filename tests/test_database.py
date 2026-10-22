from src.database import ChannelBindingRepository, Database, KeyValueRepository, ManagedMessageRepository
from src.database.connection import MIGRATIONS


def test_migrations_are_applied_once(tmp_path):
    path = tmp_path / "sous-dossier" / "bot.sqlite3"
    first = Database(path)
    ManagedMessageRepository(first).upsert("a", 1, 2, "h")
    first.close()
    second = Database(path)  # réouverture : aucune migration rejouée, données conservées
    assert second.schema_version == len(MIGRATIONS)
    assert ManagedMessageRepository(second).get("a").message_id == 2
    second.close()


def test_upsert_updates_existing_row(db):
    repo = ManagedMessageRepository(db)
    repo.upsert("a", 1, 2, "h1")
    repo.upsert("a", 1, 3, "h2")
    rows = repo.list_all()
    assert len(rows) == 1
    assert rows[0].message_id == 3
    assert rows[0].content_hash == "h2"


def test_bindings_replace_all(db):
    repo = ChannelBindingRepository(db)
    repo.replace_all({"a": 1, "b": 2})
    repo.replace_all({"a": 10})
    assert repo.get_all() == {"a": 10}


def test_key_value(db):
    repo = KeyValueRepository(db)
    assert repo.get("x") is None
    repo.set("x", "1")
    repo.set("x", "2")
    assert repo.get("x") == "2"


def test_ping(db):
    assert db.ping() is True


def test_gitlab_event_claim_is_unique(db):
    from src.database import GitLabEventRepository

    repo = GitLabEventRepository(db)
    assert repo.claim("push:a", "push") is True
    assert repo.claim("push:a", "push") is False
    assert repo.get_status("push:a") == "queued"
    repo.set_status("push:a", "sent")
    assert repo.get_status("push:a") == "sent"
    assert repo.last_received_at() is not None
    repo.release("push:a")
    assert repo.claim("push:a", "push") is True


def test_gitlab_event_purge(db):
    from src.database import GitLabEventRepository

    repo = GitLabEventRepository(db)
    repo.claim("push:old", "push")
    repo.claim("push:new", "push")
    db.conn.execute("UPDATE gitlab_events SET received_at = datetime('now', '-40 days') WHERE event_key = 'push:old'")
    assert repo.purge_older_than(30) == 1
    assert repo.get_status("push:old") is None
    assert repo.get_status("push:new") == "queued"


def test_v1_database_is_upgraded_without_losing_data(tmp_path):
    import sqlite3

    from src.database.connection import MIGRATIONS

    path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0])
    conn.execute("INSERT INTO kv_store (key, value) VALUES ('setup_confirmed_at', 'hier')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    upgraded = Database(path)
    try:
        assert upgraded.schema_version == len(MIGRATIONS)
        assert upgraded.conn.execute("SELECT value FROM kv_store").fetchone()[0] == "hier"
        upgraded.conn.execute("SELECT COUNT(*) FROM gitlab_events").fetchone()
    finally:
        upgraded.close()


# --- Lot 1 : migration v4, transactions, journal d'administration ---------------


def _v3_database(path):
    """Base telle que la V2 la laisse (schéma v3), avec des données réelles."""
    import json
    import sqlite3

    conn = sqlite3.connect(path)
    for version, script in enumerate(MIGRATIONS[:3], start=1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("INSERT INTO managed_messages (key, channel_id, message_id, content_hash) VALUES ('bienvenue', 1, 2, 'h')")
    conn.execute("INSERT INTO channel_bindings (key, channel_id) VALUES ('bienvenue', 1)")
    notification = json.dumps({"title": "📤 2 commits sur main", "url": "https://gitlab.com/x"})
    conn.execute(
        "INSERT INTO gitlab_events (event_key, event_type, status, notification) VALUES ('push:u1', 'push', 'sent', ?)",
        (notification,),
    )
    conn.commit()
    conn.close()


def test_migration_from_v3_keeps_existing_data(tmp_path):
    from src.database import GitLabEventRepository

    path = tmp_path / "v3.sqlite3"
    _v3_database(path)
    db = Database(path)
    try:
        assert db.schema_version == len(MIGRATIONS)
        message = ManagedMessageRepository(db).get("bienvenue")
        assert (message.channel_id, message.message_id, message.content_hash) == (1, 2, "h")
        assert message.last_action is None
        assert ChannelBindingRepository(db).get_all() == {"bienvenue": 1}
        record = GitLabEventRepository(db).recent()[0]
        # Les événements V2 apparaissent dans l'historique avec leur titre et leur lien.
        assert (record.title, record.url, record.status) == ("📤 2 commits sur main", "https://gitlab.com/x", "sent")
        # L'ancien identifiant de livraison reste reconnu comme doublon.
        assert not GitLabEventRepository(db).claim("push:u1", "push")
    finally:
        db.close()


def test_failed_migration_leaves_database_untouched(tmp_path, monkeypatch):
    import sqlite3

    import pytest

    from src.database import connection

    path = tmp_path / "v3.sqlite3"
    _v3_database(path)
    broken = [*MIGRATIONS[:3], "ALTER TABLE managed_messages ADD COLUMN ok TEXT; CREATE TABLE gitlab_events (x);"]
    monkeypatch.setattr(connection, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.Error):
        Database(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {row[1] for row in conn.execute("PRAGMA table_info(managed_messages)")}
        assert "ok" not in columns  # la première instruction a été annulée
        assert conn.execute("SELECT message_id FROM managed_messages").fetchone()[0] == 2
    finally:
        conn.close()
    monkeypatch.undo()
    db = Database(path)  # la vraie migration réussit ensuite
    assert db.schema_version == len(MIGRATIONS)
    db.close()


def test_newer_database_is_refused(tmp_path):
    import sqlite3

    import pytest

    path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="plus récente"):
        Database(path)


def test_record_check_never_moves_the_message(db):
    repo = ManagedMessageRepository(db)
    repo.upsert("a", 1, 2, "h", "create")
    repo.record_check("a", "error", "accès refusé")
    row = repo.get("a")
    assert (row.channel_id, row.message_id, row.content_hash) == (1, 2, "h")
    assert (row.last_action, row.last_error) == ("error", "accès refusé")
    repo.upsert("a", 1, 2, "h2", "update")
    assert repo.get("a").last_error is None
    repo.record_check("inconnu", "error", "x")  # aucune ligne créée
    assert repo.get("inconnu") is None


def test_admin_action_log(db):
    from src.database import AdminActionRepository

    log = AdminActionRepository(db)
    log.record(1, "alice", "setup", "", "19 salons")
    log.record(2, "bob", "rebuild", "bienvenue", "x" * 1000)
    actions = log.recent()
    assert [a.action for a in actions] == ["rebuild", "setup"]
    assert len(actions[0].outcome) == AdminActionRepository.MAX_TEXT


def test_recent_events_filters(db):
    from src.database import EventSummary, GitLabEventRepository

    repo = GitLabEventRepository(db)
    repo.claim("push:1", "push", summary=EventSummary(dedup_key="d1", title="p1"))
    repo.claim("pipeline:1", "pipeline", summary=EventSummary(dedup_key="d2", title="pl1", ref="main"))
    assert not repo.claim("pipeline:2", "pipeline", summary=EventSummary(dedup_key="d2"))  # même fait
    repo.set_status("push:1", "sent")
    assert [r.title for r in repo.recent(("pipeline",))] == ["pl1"]
    assert [r.title for r in repo.recent(statuses=("sent",))] == ["p1"]
    assert len(repo.recent(limit=1)) == 1
