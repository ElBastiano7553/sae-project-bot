"""Identité visuelle commune (couleurs, horodatages, liens)."""

import datetime as dt

from src.services import design


def test_tone_colors_follow_the_agreed_palette():
    assert design.tone_color(design.Tone.SUCCESS) == design.Color.SUCCESS  # vert
    assert design.tone_color(design.Tone.FAILURE) == design.Color.FAILURE  # rouge
    assert design.tone_color(design.Tone.WARNING) == design.Color.WARNING  # orange
    assert design.tone_color(design.Tone.INFO) == design.Color.INFO  # bleu
    assert design.tone_color("inconnu") == design.Color.INFO


def test_footer_is_branded_and_bounded():
    assert design.footer("a", "", "b") == "SAE Project Bot · a · b"
    assert len(design.footer("x" * 5000)) <= 2048


def test_discord_timestamps():
    moment = dt.datetime(2026, 10, 22, 15, 0, tzinfo=dt.UTC)
    assert design.discord_timestamp(moment, "R") == f"<t:{int(moment.timestamp())}:R>"
    naive = dt.datetime(2026, 10, 22, 15, 0)
    assert design.discord_timestamp(naive) == design.discord_timestamp(moment)


def test_parse_timestamp_accepts_gitlab_and_sqlite_formats():
    expected = dt.datetime(2026, 10, 22, 13, 0, tzinfo=dt.UTC)
    assert design.parse_timestamp("2026-10-22T15:00:00+02:00") == expected
    assert design.parse_timestamp("2026-10-22T13:00:00Z") == expected
    assert design.parse_timestamp("2026-10-22 13:00:00 UTC") == expected
    assert design.parse_timestamp("2026-10-22 13:00:00") == expected  # SQLite : UTC
    assert design.parse_timestamp("pas une date") is None
    assert design.parse_timestamp(None) is None
    assert design.relative_time("n'importe quoi") == "date inconnue"


def test_md_link_escapes_label():
    assert design.md_link("a*b [x]", "https://x.fr") == "[a\\*b (x)](https://x.fr)"
    assert design.md_link("texte", None) == "texte"


def test_small_formatters():
    assert design.format_duration(125) == "2 min 05 s"
    assert design.format_duration(3725) == "1 h 02 min"
    assert design.format_uptime(dt.timedelta(days=1, hours=2, minutes=3)) == "1 j 2 h 3 min"
    assert design.plural(1, "commit") == "1 commit"
    assert design.plural(3, "commit") == "3 commits"
    assert design.short_sha("da1560886d4f094c") == "da156088"
