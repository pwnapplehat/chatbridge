"""Filtering and grouping of the unified conversation list (no GUI code)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .sync import Conversation, SyncState


class StateGroup(Enum):
    """Coarse groups shown in the 'Show' filter."""

    ALL = "All conversations"
    NEEDS_SYNC = "Needs sync"
    IN_SYNC = "In sync"
    CURSOR_ONLY = "Only in Cursor"
    CLAUDE_ONLY = "Only in Claude"


NEEDS_SYNC_STATES = {SyncState.CURSOR_CHANGED, SyncState.CLAUDE_CHANGED, SyncState.BOTH_CHANGED, SyncState.UNCHECKED, SyncState.BROKEN}


@dataclass(frozen=True)
class ConversationFilter:
    """Criteria for the list. Empty drafts and subagent runs are hidden unless asked for."""

    query: str = ""
    project: str | None = None
    group: StateGroup = StateGroup.ALL
    include_subagents: bool = False
    include_empty: bool = False
    updated_since_ms: int | None = None


def conversation_matches(conv: Conversation, criteria: ConversationFilter) -> bool:
    """Whether a conversation passes every active criterion."""
    if conv.is_empty and not criteria.include_empty:
        return False
    if conv.is_subagent and not criteria.include_subagents:
        return False
    group = criteria.group
    if (
        (group is StateGroup.NEEDS_SYNC and conv.state not in NEEDS_SYNC_STATES)
        or (group is StateGroup.IN_SYNC and conv.state is not SyncState.IN_SYNC)
        or (group is StateGroup.CURSOR_ONLY and conv.state is not SyncState.CURSOR_ONLY)
        or (group is StateGroup.CLAUDE_ONLY and conv.state is not SyncState.CLAUDE_ONLY)
    ):
        return False
    if criteria.project is not None and conv.project != criteria.project:
        return False
    if criteria.updated_since_ms is not None and conv.updated_ms < criteria.updated_since_ms:
        return False
    needle = criteria.query.strip().lower()
    if needle:
        cursor_text = f"{conv.cursor.ref.preview} {conv.cursor.ref.chat_id} {conv.cursor.ref.source_label}" if conv.cursor else ""
        claude_text = f"{conv.claude.key} {conv.claude.cli_id}" if conv.claude else ""
        haystack = f"{conv.title} {conv.project} {cursor_text} {claude_text}".lower()
        return all(word in haystack for word in needle.split())
    return True


def apply_conversation_filter(conversations: list[Conversation], criteria: ConversationFilter) -> list[Conversation]:
    """Matching conversations, most recently updated first."""
    return sorted((c for c in conversations if conversation_matches(c, criteria)), key=lambda c: c.updated_ms, reverse=True)


def projects_of_conversations(conversations: list[Conversation]) -> list[str]:
    """Sorted distinct project names."""
    return sorted({c.project for c in conversations}, key=str.lower)
