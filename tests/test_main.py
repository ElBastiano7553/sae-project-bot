from src.main import check_content_files


def test_content_files_are_valid_without_gitlab():
    check_content_files()


def test_content_files_are_valid_with_gitlab():
    check_content_files(gitlab_enabled=True)


def test_broken_gitlab_rules_do_not_stop_the_bot(monkeypatch, caplog):
    from src.config import ConfigError

    def broken(**kwargs):
        raise ConfigError("gitlab.toml cassé")

    monkeypatch.setattr("src.main.load_gitlab_rules", broken)
    check_content_files(gitlab_enabled=True)
    assert "gitlab.toml cassé" in caplog.text
