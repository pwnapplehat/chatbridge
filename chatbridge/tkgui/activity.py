"""Activity page: auto-sync log and the undo list for writes made into Cursor."""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..sync import JournalEntry

MARKS = {"synced": "✔", "deferred": "⏳", "failed": "✖", "info": "i"}
LOG_LIMIT = 50
JOURNAL_LIMIT = 30


class ActivityPage(ttk.Frame):
    """Two lists: what the app did recently, and reversible Cursor writes with an Undo button."""

    def __init__(self, parent: tk.Misc, on_undo: Callable[[JournalEntry], None]) -> None:
        super().__init__(parent, padding=14)
        self._on_undo = on_undo
        self.journals: list[JournalEntry] = []

        ttk.Label(self, text="Recent activity", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self, text="Syncs run by you or by auto-sync appear here.", foreground="#707070").pack(anchor="w", pady=(0, 4))
        self.log = ttk.Treeview(self, columns=("time", "message"), show="headings", height=9, selectmode="browse")
        self.log.heading("time", text="Time")
        self.log.heading("message", text="What happened")
        self.log.column("time", width=90, stretch=False)
        self.log.column("message", width=700)
        self.log.pack(fill="both", expand=True)

        ttk.Label(self, text="Changes written into Cursor", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(16, 0))
        ttk.Label(
            self,
            text="Every message ChatBridge adds to a Cursor chat is journaled. Undo removes exactly what that sync added (close Cursor first).",
            foreground="#707070",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        self.journal_tree = ttk.Treeview(self, columns=("what", "when", "profile"), show="headings", height=8, selectmode="browse")
        self.journal_tree.heading("what", text="Change")
        self.journal_tree.heading("when", text="When")
        self.journal_tree.heading("profile", text="Cursor profile")
        self.journal_tree.column("what", width=380)
        self.journal_tree.column("when", width=140, stretch=False)
        self.journal_tree.column("profile", width=200, stretch=False)
        self.journal_tree.pack(fill="both", expand=True)
        self.journal_tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_button())
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(8, 0))
        self.undo_button = ttk.Button(bar, text="Undo selected change", command=self.undo_selected, state="disabled")
        self.undo_button.pack(side="left")
        self.set_journals([])

    def add_event(self, kind: str, message: str) -> None:
        """Prepend one line to the activity log (keeps the latest 50)."""
        self.log.insert("", 0, values=(time.strftime("%H:%M:%S"), f"{MARKS.get(kind, MARKS['info'])}  {message}"))
        children = self.log.get_children()
        for extra in children[LOG_LIMIT:]:
            self.log.delete(extra)

    def log_messages(self) -> list[str]:
        return [str(self.log.item(i, "values")[1]) for i in self.log.get_children()]

    def set_journals(self, journals: list[JournalEntry]) -> None:
        self.journals = journals[:JOURNAL_LIMIT]
        self.journal_tree.delete(*self.journal_tree.get_children())
        if not self.journals:
            self.journal_tree.insert("", "end", iid="none", values=("Nothing has been written into Cursor yet", "", ""))
        for index, entry in enumerate(self.journals):
            what = "Created chat" if entry.created else "Added to chat"
            when = time.strftime("%b %d, %H:%M", time.strptime(entry.stamp, "%Y%m%d-%H%M%S")) if len(entry.stamp) == 15 else entry.stamp
            self.journal_tree.insert(
                "", "end", iid=str(index), values=(f"{what} {entry.chat_id[:8]} · {entry.messages} messages", when, entry.profile)
            )
        self._sync_button()

    def _selected_entry(self) -> JournalEntry | None:
        picked = self.journal_tree.selection()
        return self.journals[int(picked[0])] if picked and picked[0].isdigit() else None

    def _sync_button(self) -> None:
        self.undo_button.state(["!disabled"] if self._selected_entry() is not None else ["disabled"])

    def undo_selected(self) -> None:
        entry = self._selected_entry()
        if entry is not None:
            self._on_undo(entry)
