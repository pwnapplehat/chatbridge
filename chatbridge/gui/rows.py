"""The conversation list row."""

from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .models import STATE_CSS, STATE_LABELS, ConvItem  # noqa: E402


def format_when(ms: int) -> str:
    """Short local date/time for a list subtitle."""
    return datetime.fromtimestamp(ms / 1000).strftime("%b %d, %H:%M") if ms else "unknown date"


class ConversationRow(Gtk.Box):
    """Tick box, title, subtitle, which tools hold the conversation, and its sync state."""

    __gtype_name__ = "ChatBridgeConversationRow"

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12
        )
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class("caption")
        self.subtitle.add_css_class("dim-label")
        text.append(self.title)
        text.append(self.subtitle)
        self.tools = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.cursor_pill = self._pill("Cursor")
        self.claude_pill = self._pill("Claude")
        self.tools.append(self.cursor_pill)
        self.tools.append(self.claude_pill)
        self.state = Gtk.Label(valign=Gtk.Align.CENTER)
        self.state.add_css_class("cb-chip")
        for widget in (self.check, text, self.tools, self.state):
            self.append(widget)
        self._item: ConvItem | None = None
        self._handlers: list[int] = []
        self._check_handler = self.check.connect("toggled", self._on_toggled)
        self._state_class = ""

    @staticmethod
    def _pill(name: str) -> Gtk.Label:
        label = Gtk.Label(label=name)
        label.add_css_class("cb-pill")
        return label

    def bind(self, item: ConvItem) -> None:
        self._item = item
        self._refresh(item)
        self._handlers = [item.connect("notify::selected", self._on_changed), item.connect("notify::revision", self._on_changed)]

    def unbind(self) -> None:
        if self._item is not None:
            for handler in self._handlers:
                self._item.disconnect(handler)
        self._item, self._handlers = None, []

    def _on_changed(self, item: ConvItem, _pspec: object) -> None:
        self._refresh(item)

    def _refresh(self, item: ConvItem) -> None:
        conv = item.conv
        self.title.set_text(conv.title or "(untitled)")
        records = conv.cursor.ref.record_count if conv.cursor else conv.claude.record_count if conv.claude else 0
        self.subtitle.set_text(f"{conv.project} · {format_when(conv.updated_ms)} · {records:,} records")
        self.check.handler_block(self._check_handler)
        self.check.set_active(item.selected)
        self.check.handler_unblock(self._check_handler)
        self.check.set_sensitive(item.selectable)
        for pill, present in ((self.cursor_pill, conv.cursor is not None), (self.claude_pill, conv.claude is not None)):
            (pill.remove_css_class if present else pill.add_css_class)("cb-pill-off")
            (pill.add_css_class if present else pill.remove_css_class)("cb-pill-on")
        if self._state_class:
            self.state.remove_css_class(self._state_class)
        self._state_class = STATE_CSS[conv.state]
        self.state.add_css_class(self._state_class)
        self.state.set_text(STATE_LABELS[conv.state])

    def _on_toggled(self, check: Gtk.CheckButton) -> None:
        if self._item is not None and self._item.selectable:
            self._item.selected = check.get_active()
