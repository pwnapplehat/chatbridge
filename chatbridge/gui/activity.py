"""Activity page: auto-sync log and the undo list for writes made into Cursor."""

from __future__ import annotations

import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..sync import JournalEntry  # noqa: E402

ICONS = {
    "synced": "emblem-ok-symbolic",
    "deferred": "content-loading-symbolic",
    "failed": "dialog-error-symbolic",
    "info": "dialog-information-symbolic",
}


class ActivityPage(Gtk.ScrolledWindow):
    """Two lists: what the app did recently, and reversible Cursor writes with an Undo button each."""

    __gtype_name__ = "ChatBridgeActivityPage"

    def __init__(self, on_undo: Callable[[JournalEntry], None]) -> None:
        super().__init__(vexpand=True)
        self._on_undo = on_undo
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.log_group = Adw.PreferencesGroup(title="Recent activity", description="Syncs run by you or by auto-sync appear here.")
        self.log_rows: list[Adw.ActionRow] = []
        self.journal_group = Adw.PreferencesGroup(
            title="Changes written into Cursor",
            description="Every message ChatBridge adds to a Cursor chat is journaled. Undo removes exactly what that sync added (close Cursor first).",
        )
        self.journal_rows: list[Adw.ActionRow] = []
        box.append(self.log_group)
        box.append(self.journal_group)
        self.set_child(Adw.Clamp(maximum_size=820, child=box))
        self.set_journals([])

    def add_event(self, kind: str, message: str) -> None:
        """Prepend one line to the activity log (keeps the latest 50)."""
        row = Adw.ActionRow(title=GLib.markup_escape_text(message), subtitle=time.strftime("%H:%M:%S"))
        row.set_title_lines(0)
        row.add_prefix(Gtk.Image.new_from_icon_name(ICONS.get(kind, ICONS["info"])))
        self.log_group.add(row)
        self.log_rows.append(row)
        if len(self.log_rows) > 50:
            self.log_group.remove(self.log_rows.pop(0))

    def set_journals(self, journals: list[JournalEntry]) -> None:
        for row in self.journal_rows:
            self.journal_group.remove(row)
        self.journal_rows = []
        if not journals:
            empty = Adw.ActionRow(title="Nothing has been written into Cursor yet")
            self.journal_group.add(empty)
            self.journal_rows.append(empty)
            return
        for entry in journals[:30]:
            what = "Created chat" if entry.created else "Added to chat"
            when = time.strftime("%b %d, %H:%M", time.strptime(entry.stamp, "%Y%m%d-%H%M%S")) if len(entry.stamp) == 15 else entry.stamp
            row = Adw.ActionRow(
                title=f"{what} {entry.chat_id[:8]} · {entry.messages} messages", subtitle=f"{when} · Cursor profile “{entry.profile}”"
            )
            undo = Gtk.Button(label="Undo", valign=Gtk.Align.CENTER)
            undo.connect("clicked", lambda _b, e=entry: self._on_undo(e))
            row.add_suffix(undo)
            self.journal_group.add(row)
            self.journal_rows.append(row)
