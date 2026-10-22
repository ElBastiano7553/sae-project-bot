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
