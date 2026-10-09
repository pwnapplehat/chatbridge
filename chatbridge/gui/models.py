"""GObject wrapper that lets a Conversation live in Gio list models and carry UI state."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject  # noqa: E402

from ..gui_common import STATE_HELP as STATE_HELP  # noqa: E402
from ..gui_common import STATE_LABELS as STATE_LABELS  # noqa: E402
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
