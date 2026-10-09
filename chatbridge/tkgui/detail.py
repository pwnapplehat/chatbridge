"""Right-hand detail pane: what the selected conversation is, where it lives, and what can be done with it."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..cursor_source import CursorProfile
from ..gui_common import STATE_HELP, STATE_LABELS, format_when
from ..service import PreviewLine
from ..sync import Conversation, SyncState

# Soft chip colours that keep dark text readable in light and dark Windows themes (the widget sets its own foreground).
STATE_COLORS = {
    SyncState.IN_SYNC: "#c8e8c8",
    SyncState.CURSOR_CHANGED: "#c9ddfa",
    SyncState.CLAUDE_CHANGED: "#fcdcb8",
    SyncState.BOTH_CHANGED: "#dccaf2",
    SyncState.CURSOR_ONLY: "#e4e4e4",
    SyncState.CLAUDE_ONLY: "#e4e4e4",
    SyncState.UNCHECKED: "#e4e4e4",
    SyncState.BROKEN: "#f5c6c8",
}
# Row text colours in the list (states that need attention stand out; neutral states keep the theme colour).
STATE_TEXT = {
    SyncState.IN_SYNC: "#2e7d32",
    SyncState.CURSOR_CHANGED: "#1565c0",
    SyncState.CLAUDE_CHANGED: "#b45309",
    SyncState.BOTH_CHANGED: "#7b1fa2",
    SyncState.BROKEN: "#c62828",
}
CHIP_FOREGROUND = "#1b1b1b"


def make_chip(parent: tk.Misc) -> tk.Label:
    return tk.Label(parent, text="", padx=10, pady=2, font=("Segoe UI", 9, "bold"), fg=CHIP_FOREGROUND)


class InfoGroup(ttk.LabelFrame):
    """A titled block of 'label: value' rows whose values can be selected and copied."""

    def __init__(self, parent: tk.Misc, title: str, fields: list[str]) -> None:
        super().__init__(parent, text=title, padding=(10, 6))
        self.columnconfigure(1, weight=1)
        self.note = ttk.Label(self, text="", foreground="#707070")
        self.labels: dict[str, ttk.Label] = {}
        self.values: dict[str, tk.Entry] = {}
        self.vars: dict[str, tk.StringVar] = {}
        for row, name in enumerate(fields, start=1):
            label = ttk.Label(self, text=name)
            label.grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=1)
            var = tk.StringVar()
            entry = tk.Entry(self, textvariable=var, state="readonly", relief="flat", readonlybackground=self._background())
            entry.grid(row=row, column=1, sticky="ew", pady=1)
            self.labels[name], self.values[name], self.vars[name] = label, entry, var
        self.note.grid(row=0, column=0, columnspan=2, sticky="w")
        self.note.grid_remove()

    def _background(self) -> str:
        return str(ttk.Style().lookup("TFrame", "background") or "SystemButtonFace")

    def set_present(self, present: bool, missing_note: str) -> None:
        for widget in [*self.labels.values(), *self.values.values()]:
            if present:
                widget.grid()
            else:
                widget.grid_remove()
        self.note.config(text=missing_note)
        if present:
            self.note.grid_remove()
        else:
            self.note.grid()

    def set(self, name: str, value: str) -> None:
        self.vars[name].set(value)


class DetailPane(ttk.Frame):
    """Shows an empty-state page or the details of one conversation, with its actions."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, padding=14)
        self.on_compare: Callable[[], None] = lambda: None
        self.on_sync: Callable[[], None] = lambda: None
        self.on_unlink: Callable[[], None] = lambda: None
        self.on_profile_changed: Callable[[CursorProfile | None], None] = lambda _p: None
        self._profiles: list[CursorProfile] = []
        self._busy = False
        self._broken = False

        self.empty = ttk.Frame(self)
        ttk.Label(self.empty, text="Select a conversation", font=("Segoe UI", 14, "bold")).pack(pady=(120, 4))
        ttk.Label(self.empty, text="Pick one from the list to see where it lives and sync it.", foreground="#707070").pack()

        self.body = ttk.Frame(self)
        self.title = ttk.Label(self.body, text="", font=("Segoe UI", 13, "bold"), wraplength=520, justify="left")
        self.title.pack(anchor="w")
        self.chip = make_chip(self.body)
        self.chip.pack(anchor="w", pady=(8, 4))
        self.help = ttk.Label(self.body, text="", wraplength=520, justify="left", foreground="#606060")
        self.help.pack(anchor="w")

        actions = ttk.Frame(self.body)
        actions.pack(anchor="w", pady=(12, 6))
        self.compare_button = ttk.Button(actions, text="Compare", command=lambda: self.on_compare())
        self.sync_button = ttk.Button(actions, text="Sync now", command=lambda: self.on_sync())
        self.unlink_button = ttk.Button(actions, text="Stop syncing", command=lambda: self.on_unlink())
        for button in (self.compare_button, self.sync_button, self.unlink_button):
            button.pack(side="left", padx=(0, 8))
        self.result = ttk.Label(self.body, text="", wraplength=520, justify="left")
        self.result.pack(anchor="w", pady=(0, 6))

        self.cursor_group = InfoGroup(self.body, "Cursor", ["Profile", "Project folder", "Stored records", "Last activity"])
        self.cursor_group.pack(fill="x", pady=4)
        self.target_frame = ttk.Frame(self.cursor_group)
        ttk.Label(self.target_frame, text="Create the Cursor chat in profile").pack(side="left", padx=(0, 8))
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(self.target_frame, textvariable=self.target_var, state="readonly", width=28)
        self.target_combo.pack(side="left")
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_profile_changed(self.selected_profile()))
        self.target_frame.grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.claude_group = InfoGroup(self.body, "Claude", ["Session", "Folder", "Records", "Last activity"])
        self.claude_group.pack(fill="x", pady=4)

        ttk.Label(self.body, text="First messages", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 2))
        holder = ttk.Frame(self.body)
        holder.pack(fill="both", expand=True)
        self.preview = tk.Text(
            holder, height=10, wrap="word", state="disabled", relief="solid", borderwidth=1, padx=8, pady=6, font=("Segoe UI", 10)
        )
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=scroll.set)
        self.preview.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.preview.tag_configure("you", foreground="#1f6feb", font=("Segoe UI", 10, "bold"))
        self.preview.tag_configure("role", foreground="#777777", font=("Segoe UI", 10, "bold"))
        ttk.Label(
            self.body,
            text="Syncing only ever appends: existing messages are never rewritten or deleted on either side.",
            foreground="#707070",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))
        self.bind("<Configure>", self._rewrap)
        self.show_empty()

    def _rewrap(self, event: tk.Event) -> None:
        wrap = max(260, event.width - 40)
        for label in (self.title, self.help, self.result):
            label.config(wraplength=wrap)

    # ------------------------------------------------------------------ updates
    def set_profiles(self, profiles: list[CursorProfile]) -> None:
        """Writable Cursor profiles offered when a Claude-only session is sent to Cursor."""
        self._profiles = profiles
        self.target_combo.config(values=[p.label for p in profiles])
        if profiles:
            self.target_combo.current(0)
        else:
            self.target_var.set("")

    def selected_profile(self) -> CursorProfile | None:
        index = self.target_combo.current()
        return self._profiles[index] if 0 <= index < len(self._profiles) else None

    def show_empty(self) -> None:
        self.body.pack_forget()
        self.empty.pack(fill="both", expand=True)

    def show_conversation(self, conv: Conversation) -> None:
        self.empty.pack_forget()
        self.body.pack(fill="both", expand=True)
        self.title.config(text=conv.title or "(untitled)")
        self.chip.config(text=STATE_LABELS[conv.state], bg=STATE_COLORS[conv.state])
        self.help.config(text=STATE_HELP[conv.state])
        self.result.config(text="")
        self._set_preview_text([])
        cursor, claude = conv.cursor, conv.claude
        self.cursor_group.set_present(cursor is not None, "Not in Cursor yet")
        if cursor:
            self.cursor_group.set("Profile", cursor.ref.source_label)
            self.cursor_group.set("Project folder", cursor.ref.cwd or "(no folder)")
            self.cursor_group.set("Stored records", f"{cursor.ref.record_count:,}")
            self.cursor_group.set("Last activity", format_when(cursor.ref.updated_ms or cursor.ref.created_ms))
        if cursor is None and claude is not None:
            self.target_frame.grid()
        else:
            self.target_frame.grid_remove()
        self.claude_group.set_present(claude is not None, "Not in Claude yet")
        if claude:
            self.claude_group.set("Session", claude.key)
            self.claude_group.set("Folder", claude.cwd or "(unknown)")
            self.claude_group.set("Records", f"{claude.record_count:,}")
            self.claude_group.set("Last activity", format_when(claude.updated_ms))
        both = cursor is not None and claude is not None
        if both:
            self.compare_button.pack(side="left", padx=(0, 8), before=self.sync_button)
        else:
            self.compare_button.pack_forget()
        if conv.link is not None:
            self.unlink_button.pack(side="left", padx=(0, 8))
        else:
            self.unlink_button.pack_forget()
        self.sync_button.config(text="Sync now" if both else "Import into Claude" if cursor else "Send to Cursor")
        self._broken = conv.state is SyncState.BROKEN
        self._apply_busy()

    def set_result(self, text: str) -> None:
        self.result.config(text=text)

    def _set_preview_text(self, lines: list[PreviewLine], empty_text: str = "") -> None:
        self.preview.config(state="normal")
        self.preview.delete("1.0", "end")
        if not lines and empty_text:
            self.preview.insert("end", empty_text)
        for line in lines:
            self.preview.insert("end", f"{line.role}\n", "you" if line.role == "You" else "role")
            self.preview.insert("end", f"{line.text}\n\n")
        self.preview.config(state="disabled")

    def set_preview(self, lines: list[PreviewLine]) -> None:
        self._set_preview_text(lines, "(no messages)")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._apply_busy()

    def _apply_busy(self) -> None:
        for button in (self.compare_button, self.unlink_button):
            button.state(["disabled"] if self._busy else ["!disabled"])
        self.sync_button.state(["disabled"] if (self._busy or self._broken) else ["!disabled"])
