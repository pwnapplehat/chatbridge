"""GObject wrapper that lets a Conversation live in Gio list models and carry UI state."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject  # noqa: E402

from ..sync import Conversation, SyncState  # noqa: E402


class ConvItem(GObject.Object):
    """One row of the conversation list: the conversation, whether it is ticked, and a refresh counter."""

    __gtype_name__ = "ChatBridgeConvItem"

    selected = GObject.Property(type=bool, default=False)
    revision = GObject.Property(type=int, default=0)

    def __init__(self, conv: Conversation) -> None:
        super().__init__()
        self.conv = conv

    @property
    def selectable(self) -> bool:
        """Everything except empty drafts can be ticked."""
        return not self.conv.is_empty

    def update(self, conv: Conversation) -> None:
        """Replace the conversation after a rescan/sync and tell the row widget to redraw."""
        self.conv = conv
        self.revision += 1
        if not self.selectable:
            self.selected = False


STATE_LABELS = {
    SyncState.IN_SYNC: "In sync",
    SyncState.CURSOR_CHANGED: "Cursor is ahead",
    SyncState.CLAUDE_CHANGED: "Claude is ahead",
    SyncState.BOTH_CHANGED: "Both changed",
    SyncState.CURSOR_ONLY: "Only in Cursor",
    SyncState.CLAUDE_ONLY: "Only in Claude",
    SyncState.UNCHECKED: "Not compared yet",
    SyncState.BROKEN: "Counterpart missing",
}
STATE_CSS = {
    SyncState.IN_SYNC: "cb-ok",
    SyncState.CURSOR_CHANGED: "cb-cursor",
    SyncState.CLAUDE_CHANGED: "cb-claude",
    SyncState.BOTH_CHANGED: "cb-both",
    SyncState.CURSOR_ONLY: "cb-only",
    SyncState.CLAUDE_ONLY: "cb-only",
    SyncState.UNCHECKED: "cb-unknown",
    SyncState.BROKEN: "cb-bad",
}
STATE_HELP = {
    SyncState.IN_SYNC: "Both tools have the same messages.",
    SyncState.CURSOR_CHANGED: "Cursor has messages that Claude does not. Sync will add them to the Claude session.",
    SyncState.CLAUDE_CHANGED: "Claude has messages that Cursor does not. Sync will add them to the Cursor chat.",
    SyncState.BOTH_CHANGED: "Both tools have new messages. Sync adds each side's new messages to the other; nothing is overwritten.",
    SyncState.CURSOR_ONLY: "This chat exists only in Cursor. Import it to continue the conversation in Claude.",
    SyncState.CLAUDE_ONLY: "This session exists only in Claude. Send it to Cursor to continue the conversation there.",
    SyncState.UNCHECKED: "These two are the same conversation but have not been compared yet. Compare to see what is missing.",
    SyncState.BROKEN: "One side of this link no longer exists. Stop syncing to forget the link.",
}
