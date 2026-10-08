"""Right-hand detail pane: what the selected conversation is, where it lives, and what can be done with it."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..cursor_source import CursorProfile  # noqa: E402
from ..service import PreviewLine  # noqa: E402
from ..sync import Conversation, SyncState  # noqa: E402
from .models import STATE_CSS, STATE_HELP, STATE_LABELS  # noqa: E402
from .rows import format_when  # noqa: E402


def _row(title: str, value: str = "") -> Adw.ActionRow:
    row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(value))
    row.set_subtitle_selectable(True)
    row.add_css_class("property")
    return row


class DetailPane(Gtk.Stack):
    """Shows an empty-state page or the details of one conversation, with its actions."""

    __gtype_name__ = "ChatBridgeDetailPane"

    def __init__(self) -> None:
        super().__init__()
        self.on_compare: Callable[[], None] = lambda: None
        self.on_sync: Callable[[], None] = lambda: None
        self.on_unlink: Callable[[], None] = lambda: None
        self.on_profile_changed: Callable[[CursorProfile | None], None] = lambda _p: None
        self._profiles: list[CursorProfile] = []
        self.add_named(
            Adw.StatusPage(
                icon_name="chat-bubble-text-symbolic",
                title="Select a conversation",
                description="Pick one from the list to see where it lives and sync it.",
            ),
            "empty",
        )
        self.add_named(self._build_detail(), "detail")
        self.set_visible_child_name("empty")

    # ------------------------------------------------------------------ construction
    def _build_detail(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.title = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.title.add_css_class("title-2")
        self.chip = Gtk.Label(xalign=0, halign=Gtk.Align.START)
        self.chip.add_css_class("cb-chip")
        self.help = Gtk.Label(xalign=0, wrap=True)
        self.help.add_css_class("dim-label")
        self._chip_class = ""
        box.append(self.title)
        box.append(self.chip)
        box.append(self.help)

        actions = Gtk.Box(spacing=8)
        self.compare_button = Gtk.Button(label="Compare")
        self.compare_button.set_tooltip_text("See what each side is missing, without changing anything")
        self.compare_button.connect("clicked", lambda _b: self.on_compare())
        self.sync_button = Gtk.Button(label="Sync now")
        self.sync_button.add_css_class("suggested-action")
        self.sync_button.connect("clicked", lambda _b: self.on_sync())
        self.unlink_button = Gtk.Button(label="Stop syncing")
        self.unlink_button.add_css_class("flat")
        self.unlink_button.set_tooltip_text("Forget the pairing (both conversations stay exactly as they are)")
        self.unlink_button.connect("clicked", lambda _b: self.on_unlink())
        for widget in (self.compare_button, self.sync_button, self.unlink_button):
            actions.append(widget)
        box.append(actions)
        self.result = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.result)

        self.cursor_group = Adw.PreferencesGroup(title="Cursor")
        self.cursor_profile_row = _row("Profile")
        self.cursor_folder_row = _row("Project folder")
        self.cursor_count_row = _row("Stored records")
        self.cursor_when_row = _row("Last activity")
        for row in (self.cursor_profile_row, self.cursor_folder_row, self.cursor_count_row, self.cursor_when_row):
            self.cursor_group.add(row)
        self.target_row = Adw.ComboRow(title="Create the Cursor chat in profile")
        self.target_row.connect("notify::selected", self._on_target_selected)
        self.cursor_group.add(self.target_row)
        box.append(self.cursor_group)

        self.claude_group = Adw.PreferencesGroup(title="Claude")
        self.claude_session_row = _row("Session")
        self.claude_folder_row = _row("Folder")
        self.claude_count_row = _row("Records")
        self.claude_when_row = _row("Last activity")
        for row in (self.claude_session_row, self.claude_folder_row, self.claude_count_row, self.claude_when_row):
            self.claude_group.add(row)
        box.append(self.claude_group)

        preview_title = Gtk.Label(label="First messages", xalign=0)
        preview_title.add_css_class("heading")
        box.append(preview_title)
        self.buffer = Gtk.TextBuffer()
        self.tag_you = self.buffer.create_tag("you", weight=700, foreground="#3584e4")
        self.tag_role = self.buffer.create_tag("role", weight=700, foreground="#888888")
        view = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, height_request=240)
        view.add_css_class("cb-preview")
        view.set_left_margin(10)
        view.set_right_margin(10)
        view.set_top_margin(8)
        frame = Gtk.ScrolledWindow(min_content_height=240)
        frame.set_child(view)
        box.append(frame)
        note = Gtk.Label(
            label="Syncing only ever appends: existing messages are never rewritten or deleted on either side.", xalign=0, wrap=True
        )
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        note.set_ellipsize(Pango.EllipsizeMode.NONE)
        box.append(note)
        clamp = Adw.Clamp(maximum_size=780, child=box)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(clamp)
        return scroller

    # ------------------------------------------------------------------ updates
    def set_profiles(self, profiles: list[CursorProfile]) -> None:
        """Writable Cursor profiles offered when a Claude-only session is sent to Cursor."""
        self._profiles = profiles
        self.target_row.set_model(Gtk.StringList.new([p.label for p in profiles]))

    def selected_profile(self) -> CursorProfile | None:
        index = self.target_row.get_selected()
        return self._profiles[index] if 0 <= index < len(self._profiles) else None

    def _on_target_selected(self, *_args: object) -> None:
        self.on_profile_changed(self.selected_profile())

    def show_empty(self) -> None:
        self.set_visible_child_name("empty")

    def show_conversation(self, conv: Conversation) -> None:
        self.set_visible_child_name("detail")
        self.title.set_text(conv.title or "(untitled)")
        if self._chip_class:
            self.chip.remove_css_class(self._chip_class)
        self._chip_class = STATE_CSS[conv.state]
        self.chip.add_css_class(self._chip_class)
        self.chip.set_text(STATE_LABELS[conv.state])
        self.help.set_text(STATE_HELP[conv.state])
        self.result.set_text("")
        self.buffer.set_text("")

        cursor, claude = conv.cursor, conv.claude
        self.cursor_group.set_description("" if cursor else "Not in Cursor yet")
        self.cursor_profile_row.set_visible(cursor is not None)
        self.cursor_folder_row.set_visible(cursor is not None)
        self.cursor_count_row.set_visible(cursor is not None)
        self.cursor_when_row.set_visible(cursor is not None)
        if cursor:
            self.cursor_profile_row.set_subtitle(GLib.markup_escape_text(cursor.ref.source_label))
            self.cursor_folder_row.set_subtitle(GLib.markup_escape_text(cursor.ref.cwd or "(no folder)"))
            self.cursor_count_row.set_subtitle(f"{cursor.ref.record_count:,}")
            self.cursor_when_row.set_subtitle(format_when(cursor.ref.updated_ms or cursor.ref.created_ms))
        self.target_row.set_visible(cursor is None and claude is not None)

        self.claude_group.set_description("" if claude else "Not in Claude yet")
        for row in (self.claude_session_row, self.claude_folder_row, self.claude_count_row, self.claude_when_row):
            row.set_visible(claude is not None)
        if claude:
            self.claude_session_row.set_subtitle(GLib.markup_escape_text(claude.key))
            self.claude_folder_row.set_subtitle(GLib.markup_escape_text(claude.cwd or "(unknown)"))
            self.claude_count_row.set_subtitle(f"{claude.record_count:,}")
            self.claude_when_row.set_subtitle(format_when(claude.updated_ms))

        both = cursor is not None and claude is not None
        self.compare_button.set_visible(both)
        self.unlink_button.set_visible(conv.link is not None)
        self.sync_button.set_label("Sync now" if both else "Import into Claude" if cursor else "Send to Cursor")
        self.sync_button.set_sensitive(conv.state is not SyncState.BROKEN)

    def set_result(self, text: str) -> None:
        self.result.set_text(text)

    def set_preview(self, lines: list[PreviewLine]) -> None:
        self.buffer.set_text("" if lines else "(no messages)")
        for line in lines:
            self.buffer.insert_with_tags(
                self.buffer.get_end_iter(), f"{line.role}\n", self.tag_you if line.role == "You" else self.tag_role
            )
            self.buffer.insert(self.buffer.get_end_iter(), f"{line.text}\n\n")

    def set_busy(self, busy: bool) -> None:
        for button in (self.compare_button, self.sync_button, self.unlink_button):
            button.set_sensitive(not busy)
