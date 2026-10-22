"""Contrôle d'accès aux commandes d'administration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class _Permissions(Protocol):
    manage_guild: bool
    administrator: bool


class _Role(Protocol):
    name: str


class MemberLike(Protocol):
    guild_permissions: _Permissions
    roles: Iterable[_Role]


def is_bot_admin(member: MemberLike, admin_role_name: str | None) -> bool:
    """Un membre est administrateur du bot s'il peut gérer le serveur ou possède le rôle configuré."""
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    if admin_role_name:
        wanted = admin_role_name.casefold()
        return any(role.name.casefold() == wanted for role in member.roles)
    return False
