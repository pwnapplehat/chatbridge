"""Confirmation, progress and report dialogs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

STATUS_ICONS = {
    "synced": "emblem-ok-symbolic",
    "written": "emblem-ok-symbolic",
    "noop": "emblem-default-symbolic",
    "exists": "emblem-default-symbolic",
    "dry-run": "view-reveal-symbolic",
    "deferred": "content-loading-symbolic",
    "empty": "action-unavailable-symbolic",
    "failed": "dialog-error-symbolic",
}


@dataclass(frozen=True)
class ReportRow:
    """One line of a report dialog."""

    title: str
    status: str
    detail: str


def make_confirm(heading: str, body: str, confirm_label: str, destructive: bool = False) -> Adw.AlertDialog:
    """A cancel/confirm alert. Connect to 'response'; the confirming response id is 'confirm'."""
    dialog = Adw.AlertDialog.new(heading, body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", confirm_label)
    appearance = Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED
    dialog.set_response_appearance("confirm", appearance)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    return dialog


def make_message(heading: str, body: str) -> Adw.AlertDialog:
    """A single-button information alert."""
    dialog = Adw.AlertDialog.new(heading, body)
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    return dialog


class ProgressDialog(Adw.Dialog):
    """Modal progress with a Cancel button that stops the batch between conversations."""

    __gtype_name__ = "ChatBridgeProgressDialog"

    def __init__(self, title: str, on_cancel: Callable[[], None]) -> None:
        super().__init__()
        self.set_title(title)
        self.set_content_width(460)
        self.set_can_close(False)
        self._on_cancel = on_cancel
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.bar = Gtk.ProgressBar(show_text=True)
        self.label = Gtk.Label(label="Starting…", xalign=0, wrap=True, ellipsize=3)
        self.cancel_button = Gtk.Button(label="Cancel", halign=Gtk.Align.END)
        self.cancel_button.connect("clicked", self._cancel)
        for widget in (self.bar, self.label, self.cancel_button):
            box.append(widget)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False))
        toolbar.set_content(box)
        self.set_child(toolbar)

    def update(self, done: int, total: int, text: str) -> None:
        self.bar.set_fraction(done / total if total else 0.0)
        self.bar.set_text(f"{done} of {total}")
        self.label.set_text(text)

    def _cancel(self, _button: Gtk.Button) -> None:
        self.cancel_button.set_sensitive(False)
        self.label.set_text("Cancelling after the current conversation finishes…")
        self._on_cancel()

    def finish(self) -> None:
        self.set_can_close(True)
        self.close()


class ReportDialog(Adw.Dialog):
    """Per-conversation results of a compare or sync run."""

    __gtype_name__ = "ChatBridgeReportDialog"

    def __init__(self, heading: str, summary: str, rows: list[ReportRow]) -> None:
        super().__init__()
        self.set_title(heading)
        self.set_content_width(720)
        self.set_content_height(520)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.summary_label = Gtk.Label(label=summary, xalign=0, wrap=True)
        page.append(self.summary_label)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for report in rows:
            listbox.append(self._row(report))
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(listbox)
        page.append(scroller)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.add_css_class("suggested-action")
        close.connect("clicked", lambda _b: self.close())
        page.append(close)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(page)
        self.set_child(toolbar)

    @staticmethod
    def _row(report: ReportRow) -> Adw.ActionRow:
        row = Adw.ActionRow(title=GLib.markup_escape_text(report.title), subtitle=GLib.markup_escape_text(report.detail))
        row.set_subtitle_lines(0)
        row.add_prefix(Gtk.Image.new_from_icon_name(STATUS_ICONS.get(report.status, "dialog-question-symbolic")))
        row.add_suffix(Gtk.Label(label=report.status, css_classes=["dim-label"]))
        return row
