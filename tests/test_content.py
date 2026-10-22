import datetime as dt
import re

import pytest

from src.config import ConfigError, Deadline, NextStep, Planning, load_links, load_server_layout
from src.services import text_limits as limits
from src.services.content import (
    ButtonTemplate,
    FieldTemplate,
    MessageTemplate,
    footer_for,
    load_message_templates,
    payload_length,
    render_message,
)

TODAY = dt.date(2026, 10, 22)
EMPTY_PLANNING = Planning()


def template(**overrides) -> MessageTemplate:
    base = dict(key="test", channel_key="annonces", title="Titre", description="Texte")
    return MessageTemplate(**{**base, **overrides})


def test_all_real_templates_render_and_target_known_channels():
    layout = load_server_layout()
    links = load_links()
    templates = load_message_templates()
    assert {t.key for t in templates} == {"annonces", "bienvenue", "decisions", "documentation", "planning"}
    for t in templates:
        assert t.channel_key in layout.channels
        payload = render_message(t, links, EMPTY_PLANNING, TODAY)
        assert payload.footer == footer_for(t.key)
        assert payload_length(payload) <= limits.EMBED_TOTAL


def test_team_rules_are_present_in_annonces():
    annonces = next(t for t in load_message_templates() if t.key == "annonces")
    text = " ".join(f.name + f.value for f in annonces.fields)
    for keyword in ["responsable", "GitLab", "Merge Request", "documentées", "IA", "secret"]:
        assert keyword.lower() in text.lower()


def test_link_placeholders():
    t = template(description="Dépôt : {{repo}} / Doc : {{doc}}")
    payload = render_message(t, {"repo": "https://gitlab.com/x", "doc": ""}, EMPTY_PLANNING, TODAY)
    assert "https://gitlab.com/x" in payload.description
    assert "lien à configurer" in payload.description


def test_unknown_placeholder_is_a_config_error():
    with pytest.raises(ConfigError, match="inconnu|n'existe pas"):
        render_message(template(description="{{absent}}"), {}, EMPTY_PLANNING, TODAY)


def test_buttons_without_url_are_hidden():
    t = template(buttons=(ButtonTemplate("A", "a"), ButtonTemplate("B", "b")))
    payload = render_message(t, {"a": "https://a.fr", "b": ""}, EMPTY_PLANNING, TODAY)
    assert [b.label for b in payload.buttons] == ["A"]


def test_unknown_button_link_is_a_config_error():
    with pytest.raises(ConfigError):
        render_message(template(buttons=(ButtonTemplate("A", "zzz"),)), {}, EMPTY_PLANNING, TODAY)


def test_planning_without_data_never_invents_dates():
    payload = render_message(template(dynamic="planning"), {}, EMPTY_PLANNING, TODAY)
    text = " ".join(f.value for f in payload.fields)
    assert "Aucune échéance" in text
    assert not re.search(r"\d{2}/\d{2}/\d{4}", text)


def test_planning_with_deadlines():
    planning = Planning(
        deadlines=(Deadline(dt.date(2026, 10, 1), "Passée"), Deadline(dt.date(2026, 11, 15), "Revue", "démo")),
        next_steps=(NextStep("Collisions", "Léa"), NextStep("Menu")),
    )
    payload = render_message(template(dynamic="planning"), {}, planning, TODAY)
    fields = {f.name: f.value for f in payload.fields}
    assert "15/11/2026" in fields["📌 Prochaines échéances"]
    assert "01/10/2026" in fields["✔️ Échéances passées"]
    assert "Léa" in fields["➡️ Prochaines étapes"]
    assert "responsable à désigner" in fields["➡️ Prochaines étapes"]


def test_unknown_dynamic_section():
    with pytest.raises(ConfigError):
        render_message(template(dynamic="meteo"), {}, EMPTY_PLANNING, TODAY)


def test_hash_is_stable_and_detects_changes():
    a = render_message(template(), {}, EMPTY_PLANNING, TODAY)
    b = render_message(template(), {}, EMPTY_PLANNING, TODAY)
    c = render_message(template(description="Autre texte"), {}, EMPTY_PLANNING, TODAY)
    assert a.content_hash() == b.content_hash()
    assert a.content_hash() != c.content_hash()


def test_oversized_content_respects_discord_limits():
    fields = tuple(FieldTemplate(f"Champ {i}", "x" * 3000) for i in range(30))
    t = template(title="T" * 500, description="d" * 9000, fields=fields)
    payload = render_message(t, {}, EMPTY_PLANNING, TODAY)
    assert len(payload.title) <= limits.EMBED_TITLE
    assert len(payload.description) <= limits.EMBED_DESCRIPTION
    assert len(payload.fields) <= limits.EMBED_FIELDS
    assert all(len(f.value) <= limits.FIELD_VALUE for f in payload.fields)
    assert payload_length(payload) <= limits.EMBED_TOTAL


def test_many_deadlines_are_split_into_several_fields():
    planning = Planning(deadlines=tuple(Deadline(dt.date(2027, 1, 1) + dt.timedelta(days=i), "x" * 80) for i in range(40)))
    payload = render_message(template(dynamic="planning"), {}, planning, TODAY)
    deadline_fields = [f for f in payload.fields if f.name.startswith("📌")]
    assert len(deadline_fields) > 1
    assert all(len(f.value) <= limits.FIELD_VALUE for f in payload.fields)
