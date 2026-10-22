from src.services.diagnostics import channel_uses, missing_permissions, required_permissions

ALL = {"view_channel": True, "send_messages": True, "embed_links": True, "read_message_history": True, "pin_messages": True}


def test_channel_uses_merges_messages_and_notifications():
    uses = channel_uses(["bienvenue", "annonces", "bienvenue"], ["gitlab_activite", "gitlab_activite", "annonces"])
    assert uses == {
        "bienvenue": ("messages",),
        "annonces": ("messages", "notifications"),
        "gitlab_activite": ("notifications",),
    }


def test_required_permissions_depend_on_use():
    assert "read_message_history" not in required_permissions(("notifications",), pin_messages=True)
    assert "pin_messages" in required_permissions(("messages",), pin_messages=True)
    assert "pin_messages" not in required_permissions(("messages",), pin_messages=False)


def test_missing_permissions_are_named_in_french():
    required = required_permissions(("messages",), pin_messages=True)
    assert missing_permissions(ALL, required) == []
    assert missing_permissions({**ALL, "embed_links": False}, required) == ["Intégrer des liens"]


def test_manage_messages_still_allows_pinning():
    required = required_permissions(("messages",), pin_messages=True)
    granted = {**ALL, "pin_messages": False, "manage_messages": True}
    assert missing_permissions(granted, required) == []
