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
