from pathlib import Path

import pytest

from src.config import ConfigError, load_server_layout
from src.gitlab.rules import load_gitlab_rules


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "gitlab.toml"
    path.write_text(text, encoding="utf-8")
    return path


ROUTES = '[routes]\npush = "gitlab_activite"\nmerge_request = "gitlab_activite"\npipeline = "builds"\n'


def test_real_file_is_valid_and_routes_to_existing_channels():
    layout = load_server_layout()
    rules = load_gitlab_rules(channel_keys=layout.channels.keys())
    assert rules.routes == {"push": "gitlab_activite", "merge_request": "gitlab_activite", "pipeline": "builds"}
    assert {"open", "update", "merge"} <= rules.mr_actions
    assert rules.pipeline_statuses == {"success", "failed", "canceled"}


def test_defaults_when_sections_are_missing(tmp_path):
    rules = load_gitlab_rules(write(tmp_path, ROUTES))
    assert rules.max_commits == 5
    assert rules.follows_push_branch("n-importe-quelle-branche")


def test_branch_patterns(tmp_path):
    rules = load_gitlab_rules(write(tmp_path, ROUTES + '[push]\nbranches = ["main", "feature/*"]\n'))
    assert rules.follows_push_branch("main")
    assert rules.follows_push_branch("feature/menu")
    assert not rules.follows_push_branch("experimental")


def test_missing_route(tmp_path):
    with pytest.raises(ConfigError, match="pipeline"):
        load_gitlab_rules(write(tmp_path, '[routes]\npush = "a"\nmerge_request = "a"\n'))


def test_route_to_unknown_channel(tmp_path):
    with pytest.raises(ConfigError, match="salon"):
        load_gitlab_rules(write(tmp_path, ROUTES), channel_keys={"gitlab_activite"})


@pytest.mark.parametrize(
    "extra, message",
    [
        ("[push]\nmax_commits = 0\n", "max_commits"),
        ("[push]\nmax_commits = true\n", "max_commits"),
        ('[push]\nbranches = "main"\n', "liste"),
        ('[merge_request]\nactions = ["open", "explode"]\n', "explode"),
        ('[pipeline]\nstatuses = ["fini"]\n', "fini"),
    ],
)
def test_invalid_values(tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        load_gitlab_rules(write(tmp_path, ROUTES + extra))
