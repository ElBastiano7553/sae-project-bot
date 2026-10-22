from src.config import load_server_layout
from src.services.server_structure import (
    ChannelStatus,
    GuildCategory,
    GuildChannel,
    GuildSnapshot,
    normalize_name,
    resolve_structure,
)

LAYOUT = load_server_layout()


def full_snapshot(decorate: bool = False) -> GuildSnapshot:
    """Reproduit le serveur réel. Avec decorate=True, ajoute des emojis comme sur certains serveurs."""
    categories, channels = [], []
    next_id = 1000
    for cat_spec in LAYOUT.categories.values():
        cat = GuildCategory(next_id, f"🏠 {cat_spec.name}" if decorate else cat_spec.name)
        categories.append(cat)
        next_id += 1
        for ch_spec in LAYOUT.channels.values():
            if ch_spec.category_key == cat_spec.key:
                name = f"📌・{ch_spec.name}" if decorate else ch_spec.name
                channels.append(GuildChannel(next_id, name, ch_spec.type, cat.id))
                next_id += 1
    return GuildSnapshot(tuple(categories), tuple(channels))


def by_key(report, key):
    return next(res for res in report.channels if res.spec.key == key)


def test_normalize_name():
    assert normalize_name("03 · DÉVELOPPEMENT") == "03-developpement"
    assert normalize_name("01 · ACCUEIL & ORGANISATION") == "01-accueil-organisation"
    assert normalize_name("📢・annonces") == "annonces"
    assert normalize_name("  Bilan-IA ") == "bilan-ia"


def test_complete_server_is_recognised():
    report = resolve_structure(LAYOUT, full_snapshot())
    assert report.is_complete
    assert len(report.bindings) == len(LAYOUT.channels)


def test_emojis_and_accents_are_ignored():
    report = resolve_structure(LAYOUT, full_snapshot(decorate=True))
    assert report.is_complete


def test_missing_channel_is_reported_not_created():
    snap = full_snapshot()
    channels = tuple(ch for ch in snap.channels if ch.name != "bilan-ia")
    report = resolve_structure(LAYOUT, GuildSnapshot(snap.categories, channels))
    assert by_key(report, "bilan_ia").status is ChannelStatus.MISSING
    assert "bilan_ia" not in report.bindings
    assert not report.is_complete


def test_channel_in_wrong_category_is_usable_with_warning():
    snap = full_snapshot()
    other_cat = next(c for c in snap.categories if "DÉVELOPPEMENT" in c.name)
    channels = tuple(
        GuildChannel(ch.id, ch.name, ch.type, other_cat.id) if ch.name == "annonces" else ch for ch in snap.channels
    )
    report = resolve_structure(LAYOUT, GuildSnapshot(snap.categories, channels))
    res = by_key(report, "annonces")
    assert res.status is ChannelStatus.WRONG_CATEGORY
    assert "annonces" in report.bindings


def test_duplicate_channel_prefers_expected_category():
    snap = full_snapshot()
    other_cat = snap.categories[-1]
    duplicate = GuildChannel(9999, "annonces", "text", other_cat.id)
    report = resolve_structure(LAYOUT, GuildSnapshot(snap.categories, snap.channels + (duplicate,)))
    res = by_key(report, "annonces")
    assert res.status is ChannelStatus.OK
    assert res.channel.id != 9999


def test_ambiguous_channel_is_not_bound():
    snap = full_snapshot()
    original = next(ch for ch in snap.channels if ch.name == "annonces")
    duplicate = GuildChannel(9999, "annonces", "text", original.category_id)
    report = resolve_structure(LAYOUT, GuildSnapshot(snap.categories, snap.channels + (duplicate,)))
    assert by_key(report, "annonces").status is ChannelStatus.AMBIGUOUS
    assert "annonces" not in report.bindings


def test_wrong_type_channel():
    snap = full_snapshot()
    channels = tuple(GuildChannel(ch.id, ch.name, "voice", ch.category_id) if ch.name == "code-review" else ch for ch in snap.channels)
    report = resolve_structure(LAYOUT, GuildSnapshot(snap.categories, channels))
    assert by_key(report, "code_review").status is ChannelStatus.WRONG_TYPE


def test_saved_id_keeps_renamed_channel():
    snap = full_snapshot()
    original = next(ch for ch in snap.channels if ch.name == "bienvenue")
    channels = tuple(GuildChannel(ch.id, "accueil-nouveaux", ch.type, ch.category_id) if ch.id == original.id else ch for ch in snap.channels)
    report = resolve_structure(LAYOUT, GuildSnapshot(snap.categories, channels), {"bienvenue": original.id})
    res = by_key(report, "bienvenue")
    assert res.status is ChannelStatus.OK
    assert res.channel.id == original.id
    assert "renommé" in res.note


def test_stale_saved_id_falls_back_to_name_without_duplicate():
    snap = full_snapshot()
    report = resolve_structure(LAYOUT, snap, {"bienvenue": 123})  # identifiant d'un salon supprimé
    assert by_key(report, "bienvenue").status is ChannelStatus.OK


def test_missing_category_is_reported_but_channels_still_found():
    snap = full_snapshot()
    categories = tuple(c for c in snap.categories if "VOCAL" not in c.name)
    report = resolve_structure(LAYOUT, GuildSnapshot(categories, snap.channels))
    assert [c.spec.key for c in report.missing_categories] == ["vocal"]
    res = by_key(report, "reunion")
    assert res.status is ChannelStatus.WRONG_CATEGORY and res.usable
    assert "VOCAL" in res.note
