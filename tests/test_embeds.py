"""Construction des embeds Discord, sans connexion."""

import datetime as dt

from src.config import load_links, load_planning, load_server_layout
from src.database.repositories import ManagedMessage
from src.discord_bot.embeds import (
    config_embed,
    health_embed,
    payload_to_embed,
    payload_to_view,
    structure_embed,
    sync_embed,
)
from src.services.content import LinkButton, load_message_templates, render_message
from src.services.health import build_health_checks
from src.services.permanent_messages import SyncAction, SyncResult
from src.services.server_structure import GuildSnapshot, resolve_structure


def test_permanent_message_embed_and_buttons():
    template = next(t for t in load_message_templates() if t.key == "bienvenue")
    payload = render_message(template, load_links(), load_planning(), dt.date(2026, 10, 22))
    embed = payload_to_embed(payload)
    assert embed.title == payload.title
    assert embed.footer.text == payload.footer
    assert payload_to_view(payload) is None  # aucun lien configuré : pas de bouton
    with_button = payload.__class__(**{**payload.__dict__, "buttons": (LinkButton("GitLab", "https://gitlab.com"),)})
    view = payload_to_view(with_button)
    assert view is not None and view.children[0].url == "https://gitlab.com"


def test_structure_report_embed_for_empty_server():
    report = resolve_structure(load_server_layout(), GuildSnapshot())
    embed = structure_embed(report)
    assert "salon introuvable" in embed.description
    assert len(embed) <= 6000


def test_sync_and_health_and_config_embeds():
    results = [SyncResult("bienvenue", "bienvenue", SyncAction.CREATE, 1, 2), SyncResult("x", "x", SyncAction.SKIP)]
    assert "création" in sync_embed(results, "Messages").description
    checks = build_health_checks(
        latency_ms=50, database_ok=True, guild_found=True, structure=None,
        setup_confirmed=False, ai_provider="none",
    )
    assert len(health_embed(checks, dt.timedelta(hours=26), "0.1.0").fields) == len(checks)
    embed = config_embed(
        settings_lines=["a"],
        report=resolve_structure(load_server_layout(), GuildSnapshot()),
        messages=[ManagedMessage("bienvenue", 1, 2, "h")],
        guild_id=3,
        setup_confirmed_at=None,
    )
    assert "https://discord.com/channels/3/1/2" in embed.fields[2].value


def test_gitlab_notification_embeds_respect_discord_limits():
    from src.discord_bot.embeds import notification_to_embed
    from src.gitlab.events import format_merge_request, format_pipeline, format_push
    from src.gitlab.rules import GitLabRules
    from src.gitlab.samples import merge_request_payload, pipeline_payload, push_payload

    rules = GitLabRules(routes={"push": "a", "merge_request": "a", "pipeline": "b"})
    for notification in (
        format_push(push_payload(), rules),
        format_merge_request(merge_request_payload(title="t" * 1000, description="d" * 5000), rules),
        format_pipeline(pipeline_payload(), rules),
    ):
        embed = notification_to_embed(notification)
        assert embed.title == notification.title
        assert embed.url == notification.url
        assert embed.footer.text == "GitLab · epita/sae-game"
        assert len(embed) <= 6000
