import datetime as dt
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from src.config import ConfigError, Settings, load_links, load_planning, load_server_layout, resolve_path

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
    assert list(layout.categories) == [
        "accueil", "commun", "conception", "organisation", "developpement", "livrables", "vocal", "administration",
    ]
    assert layout.channels["planning"].name == "planning"
    assert "planning-et-deadlines" in layout.channels["planning"].aliases  # ancien nom : renommé, pas recréé
    assert layout.channels["reunion"].type == "voice"
    assert layout.channels["annonces"].type == "announcement" and layout.channels["annonces"].access == "lecture"
    assert layout.categories["developpement"].name == "DÉVELOPPEMENT"
    assert layout.categories["administration"].private and layout.categories["administration"].access == "staff"
    # Chaque catégorie et chaque salon ont un emoji ; chaque salon textuel a un sujet.
    assert all(spec.emoji for spec in (*layout.categories.values(), *layout.channels.values()))
    assert all(spec.topic for spec in layout.channels.values() if spec.type in ("text", "announcement", "forum", "media"))
    # Fonctions utilisées par Samae : jamais facultatives, présentes dans tous les profils.
    for key in ("bienvenue", "annonces", "decisions", "planning", "documentation", "comptes_rendus", "gitlab_activite", "builds", "logs", "reunions"):
        assert not layout.channels[key].optional and not layout.channels[key].profiles, key
    roles = layout.roles
    assert all(role.color is not None for role in roles.values())
    assert all(not role.permissions or role.kind == "staff" for role in roles.values())
    assert {"projet", "communautaire"} <= set(layout.profiles)
    assert {"sobre", "projet", "professionnel", "communautaire", "gaming"} <= set(layout.styles)
    assert layout.policy.profile == "projet" and layout.policy.deletions_allowed


def _layout(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "server.toml"
    path.write_text('[categories.a]\nname = "A"\n[channels.x]\nname = "x"\ncategory = "a"\n' + extra, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('[roles.r]\nname = "R"\npermissions = ["administrator"]\n', "administrator"),
        ('[roles.r]\nname = "R"\npermissions = ["ban_members"]\n', "réservée aux rôles"),
        ('[roles.r]\nname = "R"\ncolor = "rouge"\n', "#RRGGBB"),
        ('[roles.r]\nname = "R"\npermissions = ["voler"]\n', "permission inconnue"),
        ('[access.p]\n[access.p.rules."role:inconnu"]\nallow = ["view_channel"]\n', "cible inconnue"),
        ('[access.p]\n[access.p.rules."@bot"]\ndeny = ["view_channel"]\n', "retirer l'accès"),
        ('[access.p]\n[access.p.rules."@everyone"]\nallow = ["send_messages"]\ndeny = ["send_messages"]\n', "à la fois"),
        ('[styles.s]\nlabel = "S"\nchannel = "{emoji}"\n', "{nom}"),
        ('[styles.s]\nlabel = "S"\nchannel = "{emoji} {nom}"\n', "espace"),
        ('[channels.y]\nname = "y"\ncategory = "a"\naccess = "absent"\n', "niveau d'accès inconnu"),
        ('[channels.y]\nname = "y"\ncategory = "a"\ntype = "fil"\n', "type doit valoir"),
        ('[refonte]\nstyle = "inexistant"\n[styles.s]\nlabel = "S"\n', "style inconnu"),
    ],
)
def test_layout_rejects_unsafe_or_invalid_redesign_settings(tmp_path: Path, extra: str, message: str):
    with pytest.raises(ConfigError, match=message):
        load_server_layout(_layout(tmp_path, extra))


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


def test_ai_settings():
    fake_key = "cle-factice-pour-test-0000"
    settings = Settings.from_env({**VALID_ENV, "AI_PROVIDER": "Anthropic", "AI_API_KEY": fake_key, "AI_MODEL": "claude-sonnet-5-5"})
    assert (settings.ai_provider, settings.ai_model, settings.ai_enabled) == ("anthropic", "claude-sonnet-5-5", True)
    assert fake_key not in repr(settings)
    assert settings.ai_daily_limit == 50 and settings.ai_cooldown_seconds == 20


@pytest.mark.parametrize(
    "env, message",
    [
        ({"AI_PROVIDER": "anthropic"}, "AI_API_KEY"),
        ({"AI_PROVIDER": "magique"}, "AI_PROVIDER"),
        ({"AI_PROVIDER": "fake", "AI_DAILY_LIMIT": "0"}, "AI_DAILY_LIMIT"),
    ],
)
def test_invalid_ai_settings_disable_ai_without_blocking_startup(env, message):
    settings = Settings.from_env({**VALID_ENV, **env})
    assert settings.ai_provider == "none" and not settings.ai_enabled
    assert message in settings.ai_config_error


def test_ai_variables_ignored_when_ai_is_off():
    settings = Settings.from_env({**VALID_ENV, "AI_DAILY_LIMIT": "abc"})
    assert settings.ai_config_error is None


def test_gitlab_api_settings():
    settings = Settings.from_env({**VALID_ENV, "GITLAB_ACCESS_TOKEN": "jeton-factice-0000", "GITLAB_PROJECT_ID": "12"})
    assert settings.gitlab_api_enabled and settings.gitlab_base_url == "https://gitlab.com"
    assert "jeton-factice-0000" not in repr(settings)
    insecure = Settings.from_env({**VALID_ENV, "GITLAB_ACCESS_TOKEN": "jeton-factice-0000", "GITLAB_PROJECT_ID": "12",
                                  "GITLAB_BASE_URL": "http://gitlab.example.com"})
    assert not insecure.gitlab_api_enabled and "https://" in insecure.gitlab_api_error


def test_backup_settings(tmp_path: Path):
    settings = Settings.from_env(VALID_ENV, project_root=tmp_path)
    assert settings.backup_dir == tmp_path / "data" / "backups"
    assert (settings.backup_keep, settings.backup_interval_hours) == (14, 24)
    custom = Settings.from_env({**VALID_ENV, "BACKUP_DIR": "/srv/sauvegardes", "BACKUP_KEEP": "3", "BACKUP_INTERVAL_HOURS": "0"})
    assert custom.backup_dir == Path("/srv/sauvegardes") and custom.backup_interval_hours == 0
    with pytest.raises(ConfigError, match="BACKUP_KEEP"):
        Settings.from_env({**VALID_ENV, "BACKUP_KEEP": "0"})


LINUX_ROOT = PurePosixPath("/home/equipe/samae")
WINDOWS_ROOT = PureWindowsPath("D:/projets/samae")


@pytest.mark.parametrize(
    "root, raw, expected",
    [
        # Linux / Docker : absolu gardé, relatif rattaché au projet.
        (LINUX_ROOT, "/srv/sauvegardes", "/srv/sauvegardes"),
        (LINUX_ROOT, "backups", "/home/equipe/samae/backups"),
        (LINUX_ROOT, "data/../backups", "/home/equipe/samae/data/../backups"),
        # Windows : chemin « Linux » (racine sans lecteur) gardé, sans le lecteur du projet.
        (WINDOWS_ROOT, "/srv/sauvegardes", "/srv/sauvegardes"),
        (WINDOWS_ROOT, "\\srv\\sauvegardes", "/srv/sauvegardes"),
        # Windows : absolus complets, sur le même lecteur, un autre lecteur ou un partage réseau.
        (WINDOWS_ROOT, "D:\\sauvegardes", "D:/sauvegardes"),
        (WINDOWS_ROOT, "E:/sauvegardes/samae", "E:/sauvegardes/samae"),
        (WINDOWS_ROOT, "\\\\nas\\partage\\samae", "//nas/partage/samae"),
        # Windows : relatifs rattachés au projet, quel que soit le séparateur.
        (WINDOWS_ROOT, "data\\backups", "D:/projets/samae/data/backups"),
        (WINDOWS_ROOT, "data/backups", "D:/projets/samae/data/backups"),
        (WINDOWS_ROOT, "..\\sauvegardes", "D:/projets/samae/../sauvegardes"),
    ],
)
def test_resolve_path(root, raw, expected):
    assert resolve_path(raw, root) == type(root)(expected)


def test_windows_semantics_behind_the_bug():
    # La cause de l'échec sous Windows : un chemin à racine sans lecteur n'est pas
    # « absolu », et la jointure lui colle le lecteur du projet.
    linux_path = PureWindowsPath("/srv/sauvegardes")
    assert not linux_path.is_absolute()
    assert WINDOWS_ROOT / linux_path == PureWindowsPath("D:/srv/sauvegardes")
    assert resolve_path(linux_path, WINDOWS_ROOT) == linux_path


@pytest.mark.parametrize(
    "env, database_path, backup_dir",
    [
        ({}, "D:/projets/samae/data/sae_bot.sqlite3", "D:/projets/samae/data/backups"),
        ({"BACKUP_DIR": "/srv/sauvegardes"}, "D:/projets/samae/data/sae_bot.sqlite3", "/srv/sauvegardes"),
        ({"BACKUP_DIR": "E:\\sauvegardes"}, "D:/projets/samae/data/sae_bot.sqlite3", "E:/sauvegardes"),
        ({"BACKUP_DIR": "sauvegardes"}, "D:/projets/samae/data/sae_bot.sqlite3", "D:/projets/samae/sauvegardes"),
        ({"DATABASE_PATH": "/srv/samae/bot.sqlite3"}, "/srv/samae/bot.sqlite3", "/srv/samae/backups"),
        ({"DATABASE_PATH": "C:\\samae\\bot.sqlite3"}, "C:/samae/bot.sqlite3", "C:/samae/backups"),
    ],
)
def test_settings_paths_with_windows_semantics(env, database_path, backup_dir):
    settings = Settings.from_env({**VALID_ENV, **env}, project_root=WINDOWS_ROOT)
    assert settings.database_path == PureWindowsPath(database_path)
    assert settings.backup_dir == PureWindowsPath(backup_dir)
    assert settings.heartbeat_path == PureWindowsPath(database_path).parent / "heartbeat"


@pytest.mark.parametrize(
    "env, database_path, backup_dir",
    [
        ({}, "/home/equipe/samae/data/sae_bot.sqlite3", "/home/equipe/samae/data/backups"),
        ({"BACKUP_DIR": "/srv/sauvegardes"}, "/home/equipe/samae/data/sae_bot.sqlite3", "/srv/sauvegardes"),
        ({"BACKUP_DIR": "sauvegardes"}, "/home/equipe/samae/data/sae_bot.sqlite3", "/home/equipe/samae/sauvegardes"),
        ({"DATABASE_PATH": "/app/data/bot.sqlite3"}, "/app/data/bot.sqlite3", "/app/data/backups"),
    ],
)
def test_settings_paths_with_linux_semantics(env, database_path, backup_dir):
    settings = Settings.from_env({**VALID_ENV, **env}, project_root=LINUX_ROOT)
    assert settings.database_path == PurePosixPath(database_path)
    assert settings.backup_dir == PurePosixPath(backup_dir)
