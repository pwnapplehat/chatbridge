"""Conversation list filtering and settings persistence."""

from __future__ import annotations

from chatbridge.config import Settings, load_settings, save_settings
from chatbridge.conversations import ConversationFilter, StateGroup, apply_conversation_filter, projects_of_conversations
from chatbridge.sync import SyncService
from tests.fixtures import CHAT_BACKUP_ONLY, CHAT_MAIN, CHAT_SUB, World
from tests.test_sync import native_session


def test_default_filter_and_groups(world: World) -> None:
    native_session(world)
    sync = SyncService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))]))
    sync.sync(next(c for c in sync.load_conversations()[0] if c.cursor and c.cursor.ref.chat_id == CHAT_MAIN), apply=True)
    conversations, _ = sync.load_conversations()
    ids = {c.cursor.ref.chat_id if c.cursor else "claude-only" for c in apply_conversation_filter(conversations, ConversationFilter())}
    assert CHAT_SUB not in ids, "subagents hidden by default"
    assert {c.state.name for c in apply_conversation_filter(conversations, ConversationFilter(group=StateGroup.IN_SYNC))} == {"IN_SYNC"}
    only_claude = apply_conversation_filter(conversations, ConversationFilter(group=StateGroup.CLAUDE_ONLY))
    assert [c.title for c in only_claude] == ["Refactor lexer"]
    only_cursor = apply_conversation_filter(conversations, ConversationFilter(group=StateGroup.CURSOR_ONLY))
    assert CHAT_BACKUP_ONLY in {c.cursor.ref.chat_id for c in only_cursor if c.cursor}
    assert [c.title for c in apply_conversation_filter(conversations, ConversationFilter(query="lexer"))] == ["Refactor lexer"]
    assert "parser-app" in projects_of_conversations(conversations)


def test_settings_roundtrip_with_auto_sync(world: World) -> None:
    save_settings(world.paths, Settings(auto_sync=True, auto_interval=45, dry_run=False))
    loaded = load_settings(world.paths)
    assert (loaded.auto_sync, loaded.auto_interval, loaded.dry_run) == (True, 45, False)
    assert load_settings(world.paths).auto_interval >= 5
