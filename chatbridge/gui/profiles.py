"""Dialog for managing extra (backup) Cursor profiles."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..config import Settings  # noqa: E402
from ..gui_common import find_user_dir  # noqa: E402


class ProfilesDialog(Adw.Dialog):
    """Lists configured backup profiles; add via folder picker, remove with one click."""

    __gtype_name__ = "ChatBridgeProfilesDialog"

    def __init__(self, parent: Gtk.Window, settings: Settings, on_changed: Callable[[], None]) -> None:
        super().__init__()
        self._parent = parent
        self._settings = settings
        self._on_changed = on_changed
        self.set_title("Cursor backup profiles")
        self.set_content_width(560)
        self.set_content_height(420)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        hint = Gtk.Label(
            label="The live Cursor profile is always scanned. Add copies of other Cursor user-data folders here (for example a backup of an old"
            " Windows install) to make their chats importable.",
            wrap=True,
            xalign=0,
        )
        hint.add_css_class("dim-label")
        page.append(hint)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.listbox)
        page.append(scroller)
        self.error_label = Gtk.Label(xalign=0, wrap=True)
        self.error_label.add_css_class("error")
        page.append(self.error_label)
        add = Gtk.Button(label="Add profile folder…", halign=Gtk.Align.START)
        add.connect("clicked", self._choose_folder)
        page.append(add)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(page)
        self.set_child(toolbar)
        self._refill()

    def _refill(self) -> None:
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)
        if not self._settings.extra_profiles:
            self.listbox.append(Adw.ActionRow(title="No backup profiles added"))
        for index, (label, path) in enumerate(self._settings.extra_profiles):
            row = Adw.ActionRow(title=GLib.markup_escape_text(label), subtitle=GLib.markup_escape_text(path))
            remove = Gtk.Button(
                icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove from this list (files are not touched)"
            )
            remove.add_css_class("flat")
            remove.connect("clicked", self._remove, index)
            row.add_suffix(remove)
            self.listbox.append(row)

    def add_path(self, chosen: Path) -> bool:
        """Validate and add a profile folder. Returns False (and shows why) when it is not a Cursor profile."""
        user_dir = find_user_dir(chosen)
        if user_dir is None:
            self.error_label.set_text(f"{chosen} does not contain globalStorage/state.vscdb, so it is not a Cursor user-data folder.")
            return False
        if any(path == str(user_dir) for _, path in self._settings.extra_profiles):
            self.error_label.set_text("That profile is already in the list.")
            return False
        label = user_dir.parent.name if user_dir.name == "User" else user_dir.name
        self._settings.extra_profiles.append((label, str(user_dir)))
        self.error_label.set_text("")
        self._refill()
        self._on_changed()
        return True

    def _remove(self, _button: Gtk.Button, index: int) -> None:
        del self._settings.extra_profiles[index]
        self._refill()
        self._on_changed()

    def _choose_folder(self, _button: Gtk.Button) -> None:
        picker = Gtk.FileDialog(title="Choose a Cursor user-data folder")
        picker.select_folder(self._parent, None, self._folder_chosen)

    def _folder_chosen(self, picker: Gtk.FileDialog, result: object) -> None:
        try:
            folder = picker.select_folder_finish(result)
        except GLib.Error:
            return  # dismissed
        if folder is not None and folder.get_path():
            self.add_path(Path(folder.get_path()))
