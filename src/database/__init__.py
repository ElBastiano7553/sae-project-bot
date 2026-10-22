"""Accès à la base SQLite (connexion, migrations, dépôts de données)."""

from src.database.connection import Database
from src.database.repositories import (
    AdminAction,
    AdminActionRepository,
    AIRequestRepository,
    AIUsage,
    BotError,
    BotErrorRepository,
    ChannelBindingRepository,
    EventRecord,
    EventSummary,
    GitLabEventRepository,
    KeyValueRepository,
    ManagedMessage,
    ManagedMessageRepository,
)
from src.database.meetings import MeetingDetail, MeetingRepository

__all__ = [
    "AdminAction",
    "AdminActionRepository",
    "AIRequestRepository",
    "AIUsage",
    "BotError",
    "BotErrorRepository",
    "ChannelBindingRepository",
    "EventRecord",
    "EventSummary",
    "Database",
    "GitLabEventRepository",
    "KeyValueRepository",
    "ManagedMessage",
    "ManagedMessageRepository",
    "MeetingDetail",
    "MeetingRepository",
]
