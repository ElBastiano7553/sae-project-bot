"""Variables MUSIC_* et LAVALINK_* : une erreur désactive la musique, jamais le reste du bot."""

from __future__ import annotations

import pytest

from src.config import SECRET_ENV_VARS, Settings, secret_values

BASE = {"DISCORD_GUILD_ID": "42"}
PASSWORD = "mot-de-passe-lavalink-0123456789"


def settings(**env) -> Settings:
    return Settings.from_env({**BASE, **env}, require_token=False)


def test_music_is_disabled_by_default():
    s = settings()
    assert not s.music_enabled and s.music_config_error is None and s.music_history_days == 90


def test_valid_music_configuration():
    s = settings(
        MUSIC_ENABLED="true",
        LAVALINK_PASSWORD=PASSWORD,
        MUSIC_SOURCES="soundcloud,youtube",
        MUSIC_DEFAULT_VOLUME="30",
        MUSIC_MAX_QUEUE="50",
        MUSIC_MAX_PLAYLIST="10",
        MUSIC_MAX_TRACK_MINUTES="0",
        MUSIC_DJ_ROLE_NAME=" DJ ",
        MUSIC_IDLE_MINUTES="2",
    )
    assert s.music_enabled and s.music_config_error is None
    assert s.lavalink_uri == "http://127.0.0.1:2333" and s.lavalink_password == PASSWORD
    assert s.music_sources == ("soundcloud", "youtube")
    assert (s.music_default_volume, s.music_max_queue, s.music_max_playlist, s.music_max_track_minutes) == (30, 50, 10, 0)
    assert s.music_dj_role_name == "DJ" and s.music_idle_minutes == 2


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"LAVALINK_PASSWORD": ""}, "LAVALINK_PASSWORD est vide"),
        ({"LAVALINK_PASSWORD": "youshallnotpass"}, "mot de passe d'exemple"),
        ({"LAVALINK_PASSWORD": "court"}, "au moins 16"),
        ({"LAVALINK_URI": "http://lavalink.exemple.fr:2333"}, "doit rester local"),
        ({"LAVALINK_URI": "ws://127.0.0.1:2333"}, "LAVALINK_URI doit être"),
        ({"LAVALINK_URI": "http://127.0.0.1"}, "port"),
        ({"LAVALINK_URI": "http://a:b@127.0.0.1:2333"}, "identifiants"),
        ({"LAVALINK_URI": "http://127.0.0.1:2333/v4"}, "LAVALINK_URI doit être"),
        ({"MUSIC_SOURCES": "napster"}, "source inconnue"),
        ({"MUSIC_DEFAULT_VOLUME": "150"}, "MUSIC_DEFAULT_VOLUME"),
        ({"MUSIC_MAX_QUEUE": "0"}, "MUSIC_MAX_QUEUE"),
        ({"MUSIC_MAX_PLAYLIST": "1000"}, "MUSIC_MAX_PLAYLIST"),
        ({"MUSIC_IDLE_MINUTES": "abc"}, "MUSIC_IDLE_MINUTES"),
    ],
)
def test_invalid_music_configuration_disables_music_only(env, fragment):
    env = {"MUSIC_ENABLED": "true", "LAVALINK_PASSWORD": PASSWORD, **env}
    s = settings(**env)
    assert not s.music_enabled and fragment in (s.music_config_error or "")
    assert s.lavalink_password == ""  # jamais gardé quand la configuration est refusée


def test_https_remote_lavalink_is_allowed():
    s = settings(MUSIC_ENABLED="true", LAVALINK_PASSWORD=PASSWORD, LAVALINK_URI="https://lavalink.exemple.fr:443/")
    assert s.music_enabled and s.lavalink_uri == "https://lavalink.exemple.fr:443"


def test_invalid_flag_is_reported():
    s = settings(MUSIC_ENABLED="peut-être")
    assert not s.music_enabled and "MUSIC_ENABLED" in s.music_config_error


def test_other_music_variables_are_ignored_when_disabled():
    s = settings(MUSIC_ENABLED="false", MUSIC_SOURCES="napster", LAVALINK_PASSWORD="x")
    assert s.music_config_error is None and not s.music_enabled


def test_lavalink_password_is_a_secret():
    assert "LAVALINK_PASSWORD" in SECRET_ENV_VARS
    assert PASSWORD in secret_values({"LAVALINK_PASSWORD": PASSWORD})
    assert PASSWORD not in repr(settings(MUSIC_ENABLED="true", LAVALINK_PASSWORD=PASSWORD))
