"""Migration v13 (musique) et dépôt de la musique, uniquement sur des bases temporaires."""

from __future__ import annotations

import sqlite3

import pytest

from src.database import Database
from src.database import connection
from src.database.connection import MIGRATIONS
from src.database.music import MAX_FAVORITES, FavoritesFull, MusicRepository
from src.music.search import normalize_track
from tests.music_fakes import raw_track

MUSIC_TABLES = {"music_history", "music_favorites", "music_guild_settings"}
MUSIC_VERSION = 13
LATEST = len(MIGRATIONS)  # version atteinte par une mise à jour (v14 et suivantes comprises)


def build_old_database(path, version: int) -> None:
    """Base au schéma ``version``, avec quelques données dans les tables de l'époque."""
    conn = sqlite3.connect(path)
    for number, script in enumerate(MIGRATIONS[:version], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute("INSERT INTO kv_store (key, value) VALUES ('setup_confirmed_at', '22/10/2026 10:00')")
    conn.execute("INSERT INTO channel_bindings (key, channel_id) VALUES ('annonces', 123)")
    if version >= 7:
        conn.execute("INSERT INTO meetings (title, leader_id, leader_name, created_by) VALUES ('Point', 1, 'Alice', 1)")
    if version >= 9:
        conn.execute(
            "INSERT INTO projects (guild_id, provider, external_id, name, created_by) VALUES (42, 'github', 'a/b', 'Jeu', 1)"
        )
    conn.commit()
    conn.close()


def tables(db: Database) -> set[str]:
    return {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def indexes(db: Database) -> set[str]:
    return {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_music_migration_is_v13_and_only_creates_tables():
    # v14 (multi-serveurs) vient après : la migration de la musique reste la v13, inchangée.
    assert len(MIGRATIONS) >= MUSIC_VERSION
    assert "music_history" in MIGRATIONS[MUSIC_VERSION - 1]
    # Uniquement des créations : aucune table existante n'est modifiée ni supprimée.
    script = MIGRATIONS[MUSIC_VERSION - 1].upper()
    assert "DROP " not in script and "ALTER " not in script and "DELETE " not in script and "UPDATE " not in script


@pytest.mark.parametrize("version", [1, 3, 7, 8, 10, 12])
def test_upgrade_keeps_existing_data_and_adds_music_tables(tmp_path, version):
    path = tmp_path / f"v{version}.sqlite3"
    build_old_database(path, version)
    db = Database(path, backup_dir=tmp_path / "backups")
    try:
        assert db.schema_version == len(MIGRATIONS)
        assert MUSIC_TABLES <= tables(db)
        assert db.conn.execute("SELECT value FROM kv_store WHERE key = 'setup_confirmed_at'").fetchone()[0] == "22/10/2026 10:00"
        assert db.conn.execute("SELECT channel_id FROM channel_bindings WHERE key = 'annonces'").fetchone()[0] == 123
        if version >= 7:
            assert db.conn.execute("SELECT title FROM meetings").fetchone()[0] == "Point"
        if version >= 9:
            assert db.conn.execute("SELECT name FROM projects").fetchone()[0] == "Jeu"
        assert db.integrity_ok()
        # Une sauvegarde vérifiée est faite avant toute migration d'une base existante.
        assert list((tmp_path / "backups").glob(f"*avantv{LATEST}*"))
    finally:
        db.close()
    # Réouverture : rien à migrer, données toujours là.
    again = Database(path, backup_dir=tmp_path / "backups")
    try:
        assert again.schema_version == LATEST
        assert again.conn.execute("SELECT COUNT(*) FROM channel_bindings").fetchone()[0] == 1
    finally:
        again.close()


def test_fresh_database_has_music_indexes_and_constraints(db):
    assert {"music_history_guild", "music_history_played_at"} <= indexes(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO music_guild_settings (guild_id, volume) VALUES (1, 101)")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO music_history (guild_id, track_key, title, source, length_ms, requested_by) VALUES (1, 'k', 't', 's', -1, 1)"
        )
    db.conn.execute(
        "INSERT INTO music_favorites (guild_id, user_id, track_key, uri, title, source) VALUES (1, 2, 'k', 'https://x.fr', 't', 's')"
    )
    with pytest.raises(sqlite3.IntegrityError):  # clé primaire : pas de doublon
        db.conn.execute(
            "INSERT INTO music_favorites (guild_id, user_id, track_key, uri, title, source) VALUES (1, 2, 'k', 'https://x.fr', 't', 's')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # un favori a toujours un lien
        db.conn.execute("INSERT INTO music_favorites (guild_id, user_id, track_key, uri, title, source) VALUES (1, 2, 'k2', NULL, 't', 's')")


def test_interrupted_music_migration_leaves_v12_untouched(tmp_path, monkeypatch):
    path = tmp_path / "v12.sqlite3"
    build_old_database(path, 12)
    broken = [*MIGRATIONS[:12], MIGRATIONS[12] + "\nCREATE TABLE projects (x);"]  # échoue après les créations musicales
    monkeypatch.setattr(connection, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.Error):
        Database(path, backup_dir=tmp_path / "backups")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not (MUSIC_TABLES & names)  # rien de la migration interrompue n'est resté
        assert conn.execute("SELECT name FROM projects").fetchone()[0] == "Jeu"
    finally:
        conn.close()
    monkeypatch.setattr(connection, "MIGRATIONS", MIGRATIONS)
    db = Database(path, backup_dir=tmp_path / "backups")  # nouvel essai au démarrage suivant
    try:
        assert db.schema_version == LATEST and MUSIC_TABLES <= tables(db)
    finally:
        db.close()


def test_a_v13_database_is_refused_by_an_older_bot(tmp_path, monkeypatch):
    path = tmp_path / "v13.sqlite3"
    Database(path).close()
    monkeypatch.setattr(connection, "MIGRATIONS", MIGRATIONS[:12])
    with pytest.raises(RuntimeError, match="plus récente"):
        Database(path)


# --- Dépôt -------------------------------------------------------------------------------


def track(title="T", **kwargs):
    return normalize_track(raw_track(title, **kwargs))


def test_history_is_per_guild_ordered_and_purged(db):
    repo = MusicRepository(db)
    repo.record_play(1, track("a"), 10)
    repo.record_play(1, track("b"), 11)
    repo.record_play(2, track("autre serveur"), 12)
    rows = repo.history(1)
    assert [r.title for r in rows] == ["b", "a"] and rows[0].requested_by == 11
    assert repo.history_count(1) == 2 and repo.history_count(2) == 1
    assert [r.title for r in repo.history(1, limit=1, offset=1)] == ["a"]
    db.conn.execute("UPDATE music_history SET played_at = datetime('now', '-100 days') WHERE title = 'a'")
    assert repo.purge_older_than(90) == 1
    assert [r.title for r in repo.history(1)] == ["b"]


def test_favorites_are_per_guild_and_user_without_duplicates(db):
    repo = MusicRepository(db)
    a = track("a", identifier="same")
    assert repo.add_favorite(1, 10, a) is True
    assert repo.add_favorite(1, 10, a) is False
    assert repo.add_favorite(1, 11, a) is True and repo.add_favorite(2, 10, a) is True
    assert [f.title for f in repo.favorites(1, 10)] == ["a"]
    assert repo.remove_favorite(1, 10, a.key) and not repo.remove_favorite(1, 10, a.key)
    assert repo.favorites(2, 10)[0].uri == a.uri
    with pytest.raises(ValueError):
        repo.add_favorite(1, 10, track("sans lien", uri=None))


def test_favorites_limit(db):
    repo = MusicRepository(db)
    for i in range(MAX_FAVORITES):
        repo.add_favorite(1, 10, track(f"t{i}", identifier=f"f{i}"))
    with pytest.raises(FavoritesFull):
        repo.add_favorite(1, 10, track("de trop", identifier="extra"))
    assert repo.add_favorite(1, 10, track("t0", identifier="f0")) is False  # déjà là : pas une erreur
    assert len(repo.favorites(1, 10)) == MAX_FAVORITES


def test_guild_volume(db):
    repo = MusicRepository(db)
    assert repo.volume(1) is None
    repo.set_volume(1, 30)
    repo.set_volume(1, 70)
    assert repo.volume(1) == 70 and repo.volume(2) is None
