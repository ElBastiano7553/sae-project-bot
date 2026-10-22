import datetime as dt
import re

import pytest

from src.config import (
    ConfigError,
    Deadline,
    LinkCatalog,
    NextStep,
    Planning,
    load_link_catalog,
    load_links,
    load_server_layout,
)
from src.services import text_limits as limits
from src.services.content import (
    MISSING_FIELD_NAME,
    Announcement,
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
    assert {t.key for t in templates} == {
        "annonce-en-cours", "annonces", "bienvenue", "decisions", "documentation", "liens", "planning"
    }
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


# --- Lot 1 : liens provisoires, message de liens, annonce ------------------------

CATALOG = LinkCatalog(
    urls={"repo": "", "doc": "", "board": "https://gitlab.com/b"},
    labels={"repo": "Dépôt", "doc": "Documentation", "board": "Tableau"},
    required=frozenset({"repo"}),
)


def test_missing_links_produce_a_provisional_section_without_inventing_urls():
    t = template(description="Dépôt : {{repo}}", buttons=(ButtonTemplate("Doc", "doc"), ButtonTemplate("Tableau", "board")))
    payload = render_message(t, CATALOG, EMPTY_PLANNING, TODAY)
    assert payload.missing_links == ("repo", "doc")
    assert payload.provisional
    section = {f.name: f.value for f in payload.fields}[MISSING_FIELD_NAME]
    assert "Dépôt — **obligatoire**" in section and "Documentation — facultatif" in section
    assert "Version provisoire" in section
    assert [b.url for b in payload.buttons] == ["https://gitlab.com/b"]
    assert "https://" not in payload.description  # aucune URL inventée


def test_completing_links_removes_the_section_and_changes_the_hash():
    t = template(description="Dépôt : {{repo}}")
    before = render_message(t, CATALOG, EMPTY_PLANNING, TODAY)
    filled = LinkCatalog(urls={**CATALOG.urls, "repo": "https://gitlab.com/r"}, labels=CATALOG.labels, required=CATALOG.required)
    after = render_message(t, filled, EMPTY_PLANNING, TODAY)
    assert not after.provisional
    assert MISSING_FIELD_NAME not in {f.name for f in after.fields}
    assert after.content_hash() != before.content_hash()  # /refresh modifiera donc le même message


def test_only_optional_links_missing_is_not_called_provisional_text():
    t = template(buttons=(ButtonTemplate("Doc", "doc"),))
    section = {f.name: f.value for f in render_message(t, CATALOG, EMPTY_PLANNING, TODAY).fields}[MISSING_FIELD_NAME]
    assert "Version provisoire" not in section and "facultatifs" in section


def test_links_message_lists_configured_and_missing_links():
    t = template(dynamic="links")
    payload = render_message(t, CATALOG, EMPTY_PLANNING, TODAY)
    fields = {f.name: f.value for f in payload.fields}
    assert fields["🔗 Liens disponibles"] == "• [Tableau](https://gitlab.com/b)"
    assert fields[MISSING_FIELD_NAME].index("Dépôt") < fields[MISSING_FIELD_NAME].index("Documentation")
    assert payload.missing_links == ("repo", "doc")


def test_announcement_message_without_announcement_shows_placeholder():
    t = template(dynamic="announcement", title="📣 Annonce en cours", description="Aucune annonce.")
    payload = render_message(t, CATALOG, EMPTY_PLANNING, TODAY)
    assert (payload.title, payload.description) == ("📣 Annonce en cours", "Aucune annonce.")


def test_announcement_message_shows_stored_announcement():
    t = template(dynamic="announcement", title="📣 Annonce en cours", description="Aucune annonce.")
    announcement = Announcement("Réunion lundi", "Salle 302 à 14 h.", "Alice", "2026-10-22T13:00:00+00:00")
    payload = render_message(t, CATALOG, EMPTY_PLANNING, TODAY, announcement=announcement)
    assert payload.title == "📣 Réunion lundi"
    assert payload.description == "Salle 302 à 14 h."
    assert "par **Alice**" in payload.fields[0].value and "<t:" in payload.fields[0].value


def test_unknown_dynamic_kind_is_rejected_when_loading(tmp_path):
    (tmp_path / "x.toml").write_text('channel = "a"\ntitle = "t"\ndescription = "d"\ndynamic = "magie"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="dynamic"):
        load_message_templates(tmp_path)


def test_real_link_catalog_declares_labels_and_required_links():
    catalog = load_link_catalog()
    assert catalog.required <= set(catalog.urls)
    assert all(key in catalog.labels for key in catalog.urls)


@pytest.mark.parametrize(
    "content, error",
    [
        ('[links]\na = "https://user:mdp@gitlab.com"\n', "identifiant"),
        ('[links]\na = "https://gitlab.com/a b"\n', "sans espace"),
        ('required = ["b"]\n[links]\na = ""\n', "required"),
        ('[links]\na = ""\n[labels]\nb = "B"\n', "labels"),
    ],
)
def test_invalid_link_catalog(tmp_path, content, error):
    path = tmp_path / "links.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=error):
        load_link_catalog(path)
