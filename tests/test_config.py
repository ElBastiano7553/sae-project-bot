import datetime as dt
from pathlib import Path

import pytest

from src.config import ConfigError, Settings, load_links, load_planning, load_server_layout

VALID_ENV = {"DISCORD_BOT_TOKEN": "faux-token-de-test", "DISCORD_GUILD_ID": "123456789012345678"}


def test_settings_valid_env(tmp_path: Path):
    settings = Settings.from_env(VALID_ENV, project_root=tmp_path)
    assert settings.guild_id == 123456789012345678
    assert settings.database_path == tmp_path / "data" / "sae_bot.sqlite3"
    assert settings.ai_provider == "none"
    assert settings.auto_refresh_on_startup is True


def test_token_is_never_in_repr():
    settings = Settings.from_env(VALID_ENV)
    assert "faux-token-de-test" not in repr(settings)


def test_missing_token_and_guild_are_reported_together():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({})
    assert "DISCORD_BOT_TOKEN" in str(exc.value)
    assert "DISCORD_GUILD_ID" in str(exc.value)


def test_token_optional_when_not_required():
    settings = Settings.from_env({"DISCORD_GUILD_ID": "42"}, require_token=False)
    assert settings.guild_id == 42


@pytest.mark.parametrize("guild_id", ["abc", "12 34", "-5"])
def test_invalid_guild_id(guild_id):
    with pytest.raises(ConfigError, match="DISCORD_GUILD_ID"):
        Settings.from_env({**VALID_ENV, "DISCORD_GUILD_ID": guild_id})


def test_boolean_values():
    settings = Settings.from_env({**VALID_ENV, "AUTO_REFRESH_ON_STARTUP": "non", "PIN_PERMANENT_MESSAGES": "False"})
    assert settings.auto_refresh_on_startup is False
    assert settings.pin_permanent_messages is False
    with pytest.raises(ConfigError, match="AUTO_REFRESH_ON_STARTUP"):
        Settings.from_env({**VALID_ENV, "AUTO_REFRESH_ON_STARTUP": "peut-être"})


def test_invalid_log_level():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env({**VALID_ENV, "LOG_LEVEL": "BAVARD"})


def test_ai_key_absent_does_not_block_startup():
    settings = Settings.from_env({**VALID_ENV, "AI_PROVIDER": "", "AI_API_KEY": ""})
    assert settings.ai_provider == "none"


def test_real_server_layout_matches_discord_structure():
    layout = load_server_layout()
    assert len(layout.categories) == 6
    assert len(layout.channels) == 19
    assert layout.channels["planning"].name == "planning-et-deadlines"
    assert layout.channels["reunion"].type == "voice"
    assert layout.categories["developpement"].name == "03 · DÉVELOPPEMENT"


def test_layout_with_unknown_category(tmp_path: Path):
    path = tmp_path / "server.toml"
    path.write_text('[categories.a]\nname = "A"\n[channels.x]\nname = "x"\ncategory = "b"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="catégorie inconnue"):
        load_server_layout(path)


def test_toml_syntax_error_is_readable(tmp_path: Path):
    path = tmp_path / "server.toml"
    path.write_text("[categories.a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Erreur de syntaxe"):
        load_server_layout(path)


def test_links_require_https(tmp_path: Path):
    path = tmp_path / "links.toml"
    path.write_text('[links]\nreadme = "http://exemple.fr"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="https://"):
        load_links(path)


def test_real_links_file_loads():
    links = load_links()
    assert "readme" in links


def test_planning_is_empty_by_default():
    planning = load_planning()
    assert planning.deadlines == ()
    assert planning.next_steps == ()


def test_planning_sorted_by_date(tmp_path: Path):
    path = tmp_path / "planning.toml"
    path.write_text(
        '[[deadlines]]\ndate = 2026-12-01\ntitle = "B"\n'
        '[[deadlines]]\ndate = 2026-11-01\ntitle = "A"\ndetails = "démo"\n'
        '[[next_steps]]\ntitle = "Faire X"\nowner = "Léa"\n',
        encoding="utf-8",
    )
    planning = load_planning(path)
    assert [d.title for d in planning.deadlines] == ["A", "B"]
    assert planning.deadlines[0].date == dt.date(2026, 11, 1)
    assert planning.next_steps[0].owner == "Léa"


def test_planning_date_in_quotes_is_rejected(tmp_path: Path):
    path = tmp_path / "planning.toml"
    path.write_text('[[deadlines]]\ndate = "2026-12-01"\ntitle = "B"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="AAAA-MM-JJ"):
        load_planning(path)


# --- V2 : webhooks GitLab ------------------------------------------------------

GITLAB_SECRET = "secret-de-test-0123456789"


def test_gitlab_disabled_by_default():
    settings = Settings.from_env(VALID_ENV)
    assert settings.gitlab_webhooks_enabled is False
    assert settings.webhook_host == "127.0.0.1"
    assert settings.webhook_port == 8080
    assert settings.gitlab_project_id is None


def test_gitlab_enabled_with_secret():
    settings = Settings.from_env(
        {**VALID_ENV, "GITLAB_WEBHOOK_SECRET": GITLAB_SECRET, "GITLAB_PROJECT_ID": "4242", "WEBHOOK_PORT": "9000"}
    )
    assert settings.gitlab_webhooks_enabled is True
    assert settings.gitlab_project_id == 4242
    assert settings.webhook_port == 9000


def test_gitlab_variables_without_secret_keep_v2_disabled():
    settings = Settings.from_env({**VALID_ENV, "GITLAB_PROJECT_ID": "4242", "GITLAB_BASE_URL": "https://gitlab.com"})
    assert settings.gitlab_webhooks_enabled is False


def test_webhook_secret_is_never_in_repr():
    settings = Settings.from_env({**VALID_ENV, "GITLAB_WEBHOOK_SECRET": GITLAB_SECRET})
    assert GITLAB_SECRET not in repr(settings)


def test_short_webhook_secret_disables_v2_without_blocking_v1():
    settings = Settings.from_env({**VALID_ENV, "GITLAB_WEBHOOK_SECRET": "court-mais-secret"[:10]})
    assert settings.gitlab_webhooks_enabled is False
    assert settings.gitlab_requested is True
    assert "trop court" in settings.gitlab_config_error
    assert "court-mais" not in settings.gitlab_config_error  # la valeur du secret n'est jamais recopiée
    assert settings.gitlab_webhook_secret == ""


@pytest.mark.parametrize("port", ["0", "70000", "http", "-1"])
def test_invalid_webhook_port_disables_v2(port):
    settings = Settings.from_env({**VALID_ENV, "GITLAB_WEBHOOK_SECRET": GITLAB_SECRET, "WEBHOOK_PORT": port})
    assert settings.gitlab_webhooks_enabled is False
    assert "WEBHOOK_PORT" in settings.gitlab_config_error


def test_invalid_project_id_disables_v2():
    settings = Settings.from_env({**VALID_ENV, "GITLAB_WEBHOOK_SECRET": GITLAB_SECRET, "GITLAB_PROJECT_ID": "epita/sae-game"})
    assert settings.gitlab_webhooks_enabled is False
    assert "GITLAB_PROJECT_ID" in settings.gitlab_config_error


def test_gitlab_variables_are_ignored_while_v2_is_not_requested():
    settings = Settings.from_env({**VALID_ENV, "WEBHOOK_PORT": "abc", "GITLAB_PROJECT_ID": "x"})
    assert settings.gitlab_config_error is None
    assert settings.gitlab_requested is False


def test_gitlab_errors_are_reported_with_v1_errors_only_for_v1():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"GITLAB_WEBHOOK_SECRET": "court"})
    assert "GITLAB" not in str(exc.value)
