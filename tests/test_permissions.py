from types import SimpleNamespace

from src.discord_bot.permissions import is_bot_admin


def member(manage_guild=False, administrator=False, roles=()):
    perms = SimpleNamespace(manage_guild=manage_guild, administrator=administrator)
    return SimpleNamespace(guild_permissions=perms, roles=[SimpleNamespace(name=r) for r in roles])


def test_manage_guild_is_admin():
    assert is_bot_admin(member(manage_guild=True), None)


def test_configured_role_is_admin_case_insensitive():
    assert is_bot_admin(member(roles=["Chef de projet"]), "chef de projet")


def test_regular_member_is_not_admin():
    assert not is_bot_admin(member(roles=["Membre"]), "Chef de projet")
    assert not is_bot_admin(member(), None)
