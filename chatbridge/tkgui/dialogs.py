"""Modal dialogs: confirmation, message, progress, report, backup profiles and About."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk

from .. import __version__
from ..config import Settings
from ..gui_common import find_user_dir

STATUS_MARKS = {
    "synced": "✔",
    "written": "✔",
    "noop": "•",
    "exists": "•",
    "dry-run": "◌",
    "deferred": "⏳",
    "empty": "-",
    "failed": "✖",
}
HEADING_FONT = ("Segoe UI", 11, "bold")


@dataclass(frozen=True)
class ReportRow:
    """One line of a report dialog."""

    title: str
    status: str
    detail: str


def _center(dialog: tk.Toplevel, parent: tk.Misc) -> None:
    dialog.update_idletasks()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - dialog.winfo_reqwidth()) // 2)
    y = parent.winfo_rooty() + max(0, (parent.winfo_height() - dialog.winfo_reqheight()) // 3)
    dialog.geometry(f"+{x}+{y}")


def _modal(parent: tk.Misc, title: str, width: int | None = None) -> tk.Toplevel:
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)
    if width:
        dialog.minsize(width, 0)
    return dialog


def ask_confirm(parent: tk.Misc, heading: str, body: str, confirm_label: str, destructive: bool = False) -> bool:
    """Cancel / confirm dialog. Returns True when confirmed. Cancel is the default button (and Escape)."""
    dialog = _modal(parent, heading, 420)
    result = {"ok": False}
    frame = ttk.Frame(dialog, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=heading, font=HEADING_FONT, wraplength=460, justify="left").pack(anchor="w")
    ttk.Label(frame, text=body, wraplength=460, justify="left").pack(anchor="w", pady=(10, 16))
    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e")

    def finish(ok: bool) -> None:
        result["ok"] = ok
        dialog.destroy()

    cancel = ttk.Button(buttons, text="Cancel", command=lambda: finish(False))
    confirm = ttk.Button(buttons, text=("⚠ " if destructive else "") + confirm_label, command=lambda: finish(True))
    cancel.pack(side="left", padx=(0, 8))
    confirm.pack(side="left")
    dialog.bind("<Escape>", lambda _e: finish(False))
    dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    cancel.focus_set()
    _center(dialog, parent)
    dialog.grab_set()
    dialog.wait_window()
    return result["ok"]


def show_message(parent: tk.Misc, heading: str, body: str) -> None:
    """Single-button information dialog."""
    dialog = _modal(parent, heading, 380)
    frame = ttk.Frame(dialog, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=heading, font=HEADING_FONT, wraplength=460, justify="left").pack(anchor="w")
    ttk.Label(frame, text=body, wraplength=460, justify="left").pack(anchor="w", pady=(10, 16))
    ok = ttk.Button(frame, text="OK", command=dialog.destroy)
    ok.pack(anchor="e")
    dialog.bind("<Return>", lambda _e: dialog.destroy())
    dialog.bind("<Escape>", lambda _e: dialog.destroy())
    ok.focus_set()
    _center(dialog, parent)
    dialog.grab_set()
    dialog.wait_window()


class ProgressDialog(tk.Toplevel):
    """Progress with a Cancel button that stops the batch between conversations."""

    def __init__(self, parent: tk.Misc, title: str, on_cancel: Callable[[], None]) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self._on_cancel = on_cancel
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        self.bar = ttk.Progressbar(frame, length=420, maximum=1.0)
        self.bar.pack(fill="x")
        self.count = ttk.Label(frame, text="Starting…")
        self.count.pack(anchor="w", pady=(8, 0))
        self.label = ttk.Label(frame, text="", wraplength=420, justify="left")
        self.label.pack(anchor="w", pady=(2, 12))
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self._cancel)
        self.cancel_button.pack(anchor="e")
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        _center(self, parent)
        self.grab_set()

    def update_progress(self, done: int, total: int, text: str) -> None:
        self.bar["value"] = done / total if total else 0.0
        self.count.config(text=f"{done} of {total}")
        self.label.config(text=text)

    def _cancel(self) -> None:
        self.cancel_button.state(["disabled"])
        self.label.config(text="Cancelling after the current conversation finishes…")
        self._on_cancel()

    def finish(self) -> None:
        if self.winfo_exists():
            self.grab_release()
            self.destroy()


class ReportDialog(tk.Toplevel):
    """Per-conversation results of a compare or sync run."""

    def __init__(self, parent: tk.Misc, heading: str, summary: str, rows: list[ReportRow]) -> None:
        super().__init__(parent)
        self.title(heading)
        self.transient(parent.winfo_toplevel())
        self.geometry("760x520")
        self.rows = rows
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.summary_label = ttk.Label(frame, text=summary, wraplength=720, justify="left")
        self.summary_label.pack(anchor="w", pady=(0, 8))
        holder = ttk.Frame(frame)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=("status", "detail"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Conversation")
        self.tree.heading("status", text="Result")
        self.tree.heading("detail", text="Details")
        self.tree.column("#0", width=300)
        self.tree.column("status", width=110, stretch=False)
        self.tree.column("detail", width=320)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for report in rows:
            mark = STATUS_MARKS.get(report.status, "?")
            self.tree.insert("", "end", text=report.title, values=(f"{mark} {report.status}", report.detail.replace("\n", "  ")))
        ttk.Button(frame, text="Close", command=self.destroy).pack(anchor="e", pady=(10, 0))
        self.bind("<Escape>", lambda _e: self.destroy())
        _center(self, parent)


class ProfilesDialog(tk.Toplevel):
    """Lists configured backup profiles; add via a folder picker, remove with one click."""

    def __init__(self, parent: tk.Misc, settings: Settings, on_changed: Callable[[], None]) -> None:
        super().__init__(parent)
        self.title("Cursor backup profiles")
        self.transient(parent.winfo_toplevel())
        self.geometry("720x440")
        self._settings = settings
        self._on_changed = on_changed
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="The live Cursor profile is always scanned. Add copies of other Cursor user-data folders here (for example a backup of an old"
            " install, or another Cursor account's data folder) to make their chats importable.",
            wraplength=680,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Add profile folder…", command=self._choose_folder).pack(side="left")
        ttk.Button(buttons, text="Remove from list", command=self._remove_selected).pack(side="left", padx=8)
        ttk.Label(buttons, text="(files are never touched)").pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.error_label = ttk.Label(frame, text="", foreground="#b00020", wraplength=660, justify="left")
        self.error_label.pack(side="bottom", anchor="w", pady=(6, 0))
        holder = ttk.Frame(frame)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=("path",), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Label")
        self.tree.heading("path", text="Folder")
        self.tree.column("#0", width=160, stretch=False)
        self.tree.column("path", width=500)
        self.tree.pack(side="left", fill="both", expand=True)
        self._refill()
        _center(self, parent)

    def _refill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if not self._settings.extra_profiles:
            self.tree.insert(
                "", "end", iid="none", text="(none)", values=("No backup profiles added yet. Use “Add profile folder…” below.",)
            )
        for index, (label, path) in enumerate(self._settings.extra_profiles):
            self.tree.insert("", "end", iid=str(index), text=label, values=(path,))

    def add_path(self, chosen: Path) -> bool:
        """Validate and add a profile folder. Returns False (and shows why) when it is not a Cursor profile."""
        user_dir = find_user_dir(chosen)
        if user_dir is None:
            self.error_label.config(text=f"{chosen} does not contain globalStorage/state.vscdb, so it is not a Cursor user-data folder.")
            return False
        if any(path == str(user_dir) for _, path in self._settings.extra_profiles):
            self.error_label.config(text="That profile is already in the list.")
            return False
        label = user_dir.parent.name if user_dir.name == "User" else user_dir.name
        self._settings.extra_profiles.append((label, str(user_dir)))
        self.error_label.config(text="")
        self._refill()
        self._on_changed()
        return True

    def remove_index(self, index: int) -> None:
        del self._settings.extra_profiles[index]
        self._refill()
        self._on_changed()

    def _remove_selected(self) -> None:
        picked = self.tree.selection()
        if picked and picked[0].isdigit():
            self.remove_index(int(picked[0]))

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="Choose a Cursor user-data folder", mustexist=True)
        if folder:
            self.add_path(Path(folder))


def show_about(parent: tk.Misc) -> None:
    show_message(
        parent,
        f"ChatBridge {__version__}",
        "Two-way sync of chat history between Cursor and Claude. Messages, reasoning and tool calls, appended safely and reversibly.\n\n"
        "MIT licensed.",
    )
