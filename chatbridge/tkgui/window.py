"""Main window: unified Cursor + Claude conversation list, detail pane, activity log, auto-sync (Tk)."""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from collections.abc import Callable
from importlib import resources
from tkinter import ttk

from ..autosync import AutoSyncer, AutoSyncEvent
from ..config import AppPaths, Settings, save_settings
from ..conversations import ConversationFilter, StateGroup, apply_conversation_filter, conversation_matches, projects_of_conversations
from ..cursor_writer import cursor_running
from ..gui_common import STATE_LABELS, describe, format_when, report_detail, summarize
from ..osenv import IS_WINDOWS
from ..service import PreviewLine
from ..sync import Conversation, JournalEntry, SyncReport, SyncService
from .activity import ActivityPage
from .bridge import Bridge
from .detail import STATE_TEXT, DetailPane
from .dialogs import ProfilesDialog, ProgressDialog, ReportDialog, ReportRow, ask_confirm, show_about, show_message

LOG = logging.getLogger(__name__)
ALL_PROJECTS = "All projects"
GROUPS = list(StateGroup)
RECENT_CHOICES = [("Any time", None), ("Last 24 hours", 1), ("Last 7 days", 7), ("Last 30 days", 30)]
CHECKED, UNCHECKED, LOCKED = "☑", "☐", "·"
POLL_MS = 4000


def report_rows(reports: list[SyncReport]) -> list[ReportRow]:
    """Convert sync reports to dialog rows."""
    return [ReportRow(r.title or r.key, r.status, report_detail(r)) for r in reports]


def enable_dpi_awareness() -> None:
    """Render crisply on high-DPI Windows displays (no effect elsewhere)."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError, ImportError):
        pass


class MainWindow(tk.Toplevel):
    """The application window (a Toplevel of a hidden root). Every user action is also a method so tests can drive it."""

    def __init__(
        self, root: tk.Tk, sync: SyncService, paths: AppPaths, settings: Settings, on_closed: Callable[[], None] | None = None
    ) -> None:
        super().__init__(root)
        self.root = root
        self._on_closed = on_closed
        self.title("ChatBridge")
        self._set_icon()
        self.geometry("1280x800")
        self.minsize(900, 560)
        self.sync_service, self.paths, self.settings = sync, paths, settings
        self.bridge = Bridge(self)
        self.criteria = ConversationFilter(include_subagents=settings.show_subagents, include_empty=settings.show_empty)
        self.autosync = AutoSyncer(sync, float(settings.auto_interval), self._autosync_event)
        self.conversations: list[Conversation] = []
        self.shown: list[Conversation] = []
        self.ticked: set[str] = set()
        self.last_reports: list[SyncReport] = []
        self.reload_count = 0
        self.busy = False
        self.report_dialog: ReportDialog | None = None
        self._preview_token = 0
        self._selected_key: str | None = None
        self._warnings: list[str] = []
        self._poll_running = False
        self._running: list[str] = []
        self._closing = False
        self._style()
        self._build_menu()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<F5>", lambda _e: self.reload())
        self.bind("<Control-f>", lambda _e: self.search_entry.focus_set())
        self.reload()
        self.after(POLL_MS, self._poll_cursor)
        if settings.auto_sync:
            self.auto_var.set(True)
            self._on_auto_toggled()

    def _set_icon(self) -> None:
        """Window / taskbar icon (Windows only; other systems keep the default)."""
        if not IS_WINDOWS:
            return
        try:
            with resources.as_file(resources.files("chatbridge").joinpath("data/chatbridge.ico")) as icon:
                self.iconbitmap(default=str(icon))  # type: ignore[no-untyped-call]
        except (OSError, tk.TclError):
            LOG.debug("window icon unavailable", exc_info=True)

    # ------------------------------------------------------------------ dialogs (overridable seams for tests)
    def confirm(self, heading: str, body: str, label: str, destructive: bool = False) -> bool:
        return ask_confirm(self, heading, body, label, destructive)

    def message(self, heading: str, body: str) -> None:
        show_message(self, heading, body)

    # ------------------------------------------------------------------ construction
    def _style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview", rowheight=26)
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"))

    def _build_menu(self) -> None:
        bar = tk.Menu(self)
        menu = tk.Menu(bar, tearoff=False)
        self.carry_var = tk.BooleanVar(value=self.settings.carry_context)
        menu.add_checkbutton(
            label="Carry model context into Cursor (experimental)", variable=self.carry_var, command=self._on_carry_toggled
        )
        menu.add_command(label="Cursor backup profiles…", command=self.show_profiles)
        menu.add_separator()
        menu.add_command(label="Rescan\tF5", command=self.reload)
        menu.add_command(label="About ChatBridge", command=lambda: show_about(self))
        menu.add_separator()
        menu.add_command(label="Exit", command=self.close)
        bar.add_cascade(label="Menu", menu=menu)
        self.config(menu=bar)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        self.refresh_button = ttk.Button(top, text="⟳ Rescan", command=self.reload)
        self.refresh_button.pack(side="left")
        self.auto_var = tk.BooleanVar(value=False)
        self.auto_check = ttk.Checkbutton(top, text="Auto-sync", variable=self.auto_var, command=self._on_auto_toggled)
        self.auto_check.pack(side="right")
        self.banner = tk.Label(self, text="", anchor="w", padx=12, pady=6, bg="#fff4ce", fg="#4a3b00", justify="left")
        self.banner_visible = False

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8)
        conversations = self.conversations_page = ttk.Frame(self.notebook)
        self.activity = ActivityPage(self.notebook, self.request_undo)
        self.notebook.add(conversations, text="  Conversations  ")
        self.notebook.add(self.activity, text="  Activity  ")
        self._build_conversations_page(conversations)

        self.bar = ttk.Frame(self, padding=(10, 8))
        self.bar.pack(fill="x", side="bottom")
        self.selected_label = ttk.Label(self.bar, text="0 selected")
        self.selected_label.pack(side="left")
        ttk.Button(self.bar, text="Select all shown", command=self.select_all_shown).pack(side="left", padx=(12, 4))
        ttk.Button(self.bar, text="Clear", command=self.clear_selection).pack(side="left")
        self.sync_selected_button = ttk.Button(self.bar, text="Sync selected…", style="Accent.TButton", command=self.sync_selected)
        self.sync_selected_button.pack(side="right")
        self.compare_selected_button = ttk.Button(self.bar, text="Compare selected", command=self.compare_selected)
        self.compare_selected_button.pack(side="right", padx=8)
        self.toast_label = ttk.Label(self.bar, text="", foreground="#1a6b1a")
        self.toast_label.pack(side="right", padx=16)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab())
        self.update_selection_label()

    def _build_conversations_page(self, page: ttk.Frame) -> None:
        paned = ttk.PanedWindow(page, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=6)
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.detail = DetailPane(paned)
        self.detail.on_compare = self.compare_focused
        self.detail.on_sync = self.sync_focused
        self.detail.on_unlink = self.unlink_focused
        paned.add(self.detail, weight=2)

        filters = ttk.Frame(left, padding=(2, 4))
        filters.pack(fill="x")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(filters, textvariable=self.search_var)
        self.search_entry.pack(fill="x")
        self.search_var.trace_add("write", lambda *_: self._update_criteria())
        row = ttk.Frame(filters)
        row.pack(fill="x", pady=(6, 0))
        self.group_var = tk.StringVar(value=GROUPS[0].value)
        self.project_var = tk.StringVar(value=ALL_PROJECTS)
        self.recent_var = tk.StringVar(value=RECENT_CHOICES[0][0])
        self.group_combo = ttk.Combobox(row, textvariable=self.group_var, values=[g.value for g in GROUPS], state="readonly", width=18)
        self.project_combo = ttk.Combobox(row, textvariable=self.project_var, values=[ALL_PROJECTS], state="readonly")
        self.recent_combo = ttk.Combobox(
            row, textvariable=self.recent_var, values=[label for label, _ in RECENT_CHOICES], state="readonly", width=14
        )
        self.group_combo.pack(side="left")
        self.project_combo.pack(side="left", fill="x", expand=True, padx=6)
        self.recent_combo.pack(side="left")
        for combo in (self.group_combo, self.project_combo, self.recent_combo):
            combo.bind("<<ComboboxSelected>>", lambda _e: self._update_criteria())
        toggles = ttk.Frame(filters)
        toggles.pack(fill="x", pady=(6, 0))
        self.subagent_var = tk.BooleanVar(value=self.settings.show_subagents)
        self.empty_var = tk.BooleanVar(value=self.settings.show_empty)
        ttk.Checkbutton(toggles, text="Subagent chats", variable=self.subagent_var, command=self._update_criteria).pack(side="left")
        ttk.Checkbutton(toggles, text="Empty drafts", variable=self.empty_var, command=self._update_criteria).pack(side="left", padx=16)

        holder = ttk.Frame(left)
        holder.pack(fill="both", expand=True, pady=(6, 0))
        columns = ("sel", "title", "project", "when", "records", "tools", "state")
        self.tree = ttk.Treeview(holder, columns=columns, show="headings", selectmode="browse")
        widths = {"sel": 34, "title": 300, "project": 120, "when": 100, "records": 70, "tools": 120, "state": 130}
        heads = {
            "sel": CHECKED,
            "title": "Conversation",
            "project": "Project",
            "when": "Updated",
            "records": "Records",
            "tools": "In",
            "state": "State",
        }
        for name in columns:
            self.tree.heading(name, text=heads[name])
            self.tree.column(name, width=widths[name], stretch=name == "title", anchor="center" if name in ("sel", "records") else "w")
        self.tree.heading("sel", text=CHECKED, command=self.toggle_all_shown)
        for state, color in STATE_TEXT.items():
            self.tree.tag_configure(state.name, foreground=color)
        self.tree.tag_configure("empty", foreground="#999999")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_focus_changed())
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_space)
        self.status_label = ttk.Label(left, text="Scanning Cursor and Claude…", foreground="#707070")
        self.status_label.pack(anchor="w", pady=(4, 0))

    # ------------------------------------------------------------------ toast / banner
    def toast(self, message: str, ms: int = 4000) -> None:
        self.toast_label.config(text=message)
        self.after(ms, lambda: self.toast_label.config(text="") if self.toast_label.cget("text") == message else None)

    def _show_banner(self, text: str | None) -> None:
        if text:
            self.banner.config(text=text)
            if not self.banner_visible:
                self.banner.pack(fill="x", before=self.notebook)
                self.banner_visible = True
        elif self.banner_visible:
            self.banner.pack_forget()
            self.banner_visible = False

    def _on_tab(self) -> None:
        if str(self.notebook.select()) == str(self.conversations_page):  # type: ignore[no-untyped-call]
            self.bar.pack(fill="x", side="bottom")
        else:
            self.bar.pack_forget()

    # ------------------------------------------------------------------ catalog
    def reload(self) -> None:
        """Rescan both tools in the background."""
        self.status_label.config(text="Scanning Cursor and Claude…")
        self.refresh_button.state(["disabled"])
        self.bridge.run_async(self.sync_service.load_conversations, self._loaded, self._load_failed)

    def _loaded(self, result: tuple[list[Conversation], list[str]]) -> None:
        conversations, warnings = result
        self.reload_count += 1
        self.conversations = conversations
        selectable = {c.key for c in conversations if not c.is_empty}
        self.ticked &= selectable
        names = projects_of_conversations(conversations)
        keep = self.project_var.get()
        self.project_combo.config(values=[ALL_PROJECTS, *names])
        if keep not in names:
            self.project_var.set(ALL_PROJECTS)
        self._warnings = warnings
        self.refresh_button.state(["!disabled"])
        self.detail.set_profiles(self.sync_service.writable_profiles())
        self._update_criteria()
        self.refresh_journals()
        self._refresh_banner()
        self._restore_focus()

    def _load_failed(self, exc: BaseException) -> None:
        self.refresh_button.state(["!disabled"])
        self.status_label.config(text=f"Could not read Cursor / Claude data: {type(exc).__name__}: {exc}")

    def all_items(self) -> list[Conversation]:
        return list(self.conversations)

    def shown_items(self) -> list[Conversation]:
        return list(self.shown)

    def selected_items(self) -> list[Conversation]:
        return [c for c in self.conversations if c.key in self.ticked]

    def focused_item(self) -> Conversation | None:
        picked = self.tree.selection()
        if not picked:
            return None
        return next((c for c in self.shown if c.key == picked[0]), None)

    def focus_conversation(self, conv: Conversation) -> None:
        """Select a row (clears filters that would hide it)."""
        if all(c.key != conv.key for c in self.shown):
            self.search_var.set("")
            self._update_criteria()
        if self.tree.exists(conv.key):
            self.tree.selection_set(conv.key)
            self.tree.see(conv.key)
            self.update_idletasks()
            self._on_focus_changed()

    def _restore_focus(self) -> None:
        if self._selected_key is not None and self.tree.exists(self._selected_key):
            self.tree.selection_set(self._selected_key)

    # ------------------------------------------------------------------ filtering / rendering
    def _update_criteria(self) -> None:
        project = self.project_var.get()
        recent = dict(RECENT_CHOICES).get(self.recent_var.get())
        group = next((g for g in GROUPS if g.value == self.group_var.get()), GROUPS[0])
        self.criteria = ConversationFilter(
            query=self.search_var.get(),
            project=None if project == ALL_PROJECTS else project,
            group=group,
            include_subagents=self.subagent_var.get(),
            include_empty=self.empty_var.get(),
            updated_since_ms=None if recent is None else int((time.time() - recent * 86400) * 1000),
        )
        self._render()
        self._save_settings()

    def _render(self) -> None:
        self.shown = apply_conversation_filter(self.conversations, self.criteria)
        focus = self._selected_key
        self.tree.delete(*self.tree.get_children())
        for conv in self.shown:
            self.tree.insert(
                "", "end", iid=conv.key, values=self._row_values(conv), tags=(conv.state.name, *(("empty",) if conv.is_empty else ()))
            )
        if focus is not None and self.tree.exists(focus):
            self.tree.selection_set(focus)
        total = len(self.conversations)
        self.status_label.config(text=f"{len(self.shown)} of {total} conversations shown" if total else "No conversations found.")
        self.update_selection_label()

    def _row_values(self, conv: Conversation) -> tuple[str, ...]:
        records = conv.cursor.ref.record_count if conv.cursor else conv.claude.record_count if conv.claude else 0
        mark = LOCKED if conv.is_empty else (CHECKED if conv.key in self.ticked else UNCHECKED)
        tools = " + ".join(name for name, present in (("Cursor", conv.cursor), ("Claude", conv.claude)) if present)
        return (
            mark,
            conv.title or "(untitled)",
            conv.project,
            format_when(conv.updated_ms),
            f"{records:,}",
            tools,
            STATE_LABELS[conv.state],
        )

    def _save_settings(self) -> None:
        self.settings.show_subagents = self.subagent_var.get()
        self.settings.show_empty = self.empty_var.get()
        self.settings.auto_sync = self.auto_var.get() if hasattr(self, "auto_var") else self.settings.auto_sync
        try:
            save_settings(self.paths, self.settings)
        except OSError:
            LOG.exception("could not save settings")

    # ------------------------------------------------------------------ selection
    def _toggle(self, conv: Conversation) -> None:
        if conv.is_empty:
            return
        self.ticked.symmetric_difference_update({conv.key})
        if self.tree.exists(conv.key):
            self.tree.set(conv.key, "sel", CHECKED if conv.key in self.ticked else UNCHECKED)
        self.update_selection_label()

    def _on_tree_click(self, event: tk.Event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#1":
            return None
        key = self.tree.identify_row(event.y)
        conv = next((c for c in self.shown if c.key == key), None)
        if conv is not None:
            self._toggle(conv)
        return "break"

    def _on_space(self, _event: tk.Event) -> str:
        conv = self.focused_item()
        if conv is not None:
            self._toggle(conv)
        return "break"

    def toggle_all_shown(self) -> None:
        selectable = [c for c in self.shown if not c.is_empty]
        if selectable and all(c.key in self.ticked for c in selectable):
            self.clear_selection()
        else:
            self.select_all_shown()

    def select_all_shown(self) -> None:
        for conv in self.shown:
            if not conv.is_empty:
                self.ticked.add(conv.key)
        self._refresh_marks()

    def clear_selection(self) -> None:
        self.ticked.clear()
        self._refresh_marks()

    def _refresh_marks(self) -> None:
        for conv in self.shown:
            if self.tree.exists(conv.key):
                self.tree.set(conv.key, "sel", self._row_values(conv)[0])
        self.update_selection_label()

    def update_selection_label(self) -> None:
        chosen = self.selected_items()
        hidden = sum(1 for c in chosen if not conversation_matches(c, self.criteria))
        self.selected_label.config(text=f"{len(chosen)} selected" + (f" ({hidden} hidden by filters)" if hidden else ""))
        state = ["!disabled"] if chosen and not self.busy else ["disabled"]
        self.sync_selected_button.state(state)
        self.compare_selected_button.state(state)

    # ------------------------------------------------------------------ focus / detail
    def _on_focus_changed(self) -> None:
        conv = self.focused_item()
        self._preview_token += 1
        if conv is None:
            return
        self._selected_key = conv.key
        self.detail.show_conversation(conv)
        self.detail.set_busy(self.busy)
        token = self._preview_token
        self.bridge.run_async(lambda: self.sync_service.preview(conv), lambda lines: self._preview_ready(token, lines), lambda _exc: None)

    def _preview_ready(self, token: int, lines: list[PreviewLine]) -> None:
        if token == self._preview_token:
            self.detail.set_preview(lines)

    # ------------------------------------------------------------------ single-conversation actions
    def compare_focused(self) -> None:
        conv = self.focused_item()
        if conv is None or self.busy:
            return
        self._set_busy(True)
        self.detail.set_result("Comparing…")
        profile = self.detail.selected_profile()
        self.bridge.run_async(
            lambda: self.sync_service.sync(conv, "both", apply=False, target_profile=profile), self._compared, self._failed
        )

    def _compared(self, report: SyncReport) -> None:
        self._set_busy(False)
        self.detail.set_result(describe(report))

    @staticmethod
    def describe(report: SyncReport) -> str:
        return describe(report)

    def sync_focused(self) -> None:
        """Dry-run first so the confirmation can say exactly what will happen."""
        conv = self.focused_item()
        if conv is None or self.busy:
            return
        profile = self.detail.selected_profile()
        self._set_busy(True)
        self.bridge.run_async(
            lambda: self.sync_service.sync(conv, "both", apply=False, target_profile=profile),
            lambda report: self._confirm_single(conv, profile, report),
            self._failed,
        )

    def _confirm_single(self, conv: Conversation, profile: object, report: SyncReport) -> None:
        self._set_busy(False)
        if report.status == "failed":
            self.message("Cannot sync this conversation", report.detail)
            return
        if report.to_claude == 0 and report.to_cursor == 0 and not report.fixes:
            self.toast("Already in sync")
            self.detail.set_result(describe(report))
            return
        notes = []
        if report.to_cursor or report.fixes:
            notes.append("Cursor must be closed on the target profile; if it is open the Cursor part is deferred, not lost.")
        if report.to_claude:
            notes.append("Claude: reopen the session (or restart the app) to see the new messages.")
        if any("compact the Claude session" in fix for fix in report.fixes):
            notes.append("Claude: quit the app (or close this session) BEFORE syncing, otherwise it keeps using its old in-memory history.")
        if self.confirm(f"Sync “{conv.title or 'conversation'}”?", describe(report) + "\n\n" + "\n".join(notes), "Sync"):
            self._apply_single(conv, profile)

    def _apply_single(self, conv: Conversation, profile: object) -> None:
        self._set_busy(True)
        self.detail.set_result("Syncing…")
        self.bridge.run_async(
            lambda: self.sync_service.sync(conv, "both", apply=True, target_profile=profile),  # type: ignore[arg-type]
            self._single_done,
            self._failed,
        )

    def _single_done(self, report: SyncReport) -> None:
        self._set_busy(False)
        self.last_reports = [report]
        kind = "failed" if report.status == "failed" else report.status if report.status in ("synced", "deferred") else "info"
        self.activity.add_event(kind, f"{report.title}: {describe(report) if report.status != 'deferred' else report.detail}")
        self.detail.set_result(report.detail if report.status in ("failed", "deferred") else f"Synced. {describe(report)}")
        if report.status == "failed":
            self.message("Sync failed", report.detail)
        else:
            self.toast("Synced" if report.status == "synced" else "Waiting: " + report.detail)
        self.reload()

    def unlink_focused(self) -> None:
        conv = self.focused_item()
        if conv is None:
            return
        if self.confirm(
            "Stop syncing this conversation?",
            "The pairing is forgotten. The Cursor chat and the Claude session both stay exactly as they are.",
            "Stop syncing",
            destructive=True,
        ):
            self._do_unlink(conv)

    def _do_unlink(self, conv: Conversation) -> None:
        self.sync_service.unlink(conv)
        self.toast("Pairing removed")
        self.reload()

    # ------------------------------------------------------------------ bulk actions
    def compare_selected(self) -> None:
        self._run_bulk(apply=False)

    def sync_selected(self) -> None:
        items = self.selected_items()
        if not items or self.busy:
            return
        if self.confirm(
            f"Sync {len(items)} conversation{'s' if len(items) != 1 else ''}?",
            "Each conversation gets the messages the other tool is missing. Conversations that exist in only one tool are created in the other. "
            "Nothing is overwritten or deleted. Use “Compare selected” first to preview.",
            "Sync",
        ):
            self._run_bulk(apply=True)

    def _run_bulk(self, apply: bool) -> None:
        convs = self.selected_items()
        if not convs or self.busy:
            return
        cancel = threading.Event()
        progress = ProgressDialog(self, "Syncing" if apply else "Comparing", cancel.set)
        self._set_busy(True)
        titles = {c.key: c.title for c in convs}
        profile = self.detail.selected_profile()
        self.bridge.run_async(
            lambda: self.sync_service.sync_many(
                convs,
                "both",
                apply,
                lambda p: self.bridge.call(lambda: progress.update_progress(p.done, p.total, titles.get(p.report.key, p.report.title))),
                cancel.is_set,
                profile,
            ),
            lambda reports: self._bulk_done(convs, reports, apply, progress),
            lambda exc: self._failed(exc, progress),
        )

    def _bulk_done(self, convs: list[Conversation], reports: list[SyncReport], apply: bool, progress: ProgressDialog) -> None:
        progress.finish()
        self._set_busy(False)
        self.last_reports = reports
        summary = summarize(reports, len(convs) - len(reports))
        if apply and any(r.to_claude for r in reports):
            summary += "\nReopen sessions in the Claude app to see new messages."
        self.report_dialog = ReportDialog(self, "Sync finished" if apply else "Comparison", summary, report_rows(reports))
        if apply:
            self.activity.add_event("synced", f"Manual sync: {summary.splitlines()[0]}")
            self.reload()

    def _failed(self, exc: BaseException, progress: ProgressDialog | None = None) -> None:
        if progress is not None:
            progress.finish()
        self._set_busy(False)
        self.message("Something went wrong", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ undo (Cursor journals)
    def refresh_journals(self) -> None:
        self.activity.set_journals(self.sync_service.list_journals())

    def request_undo(self, entry: JournalEntry) -> None:
        if self.confirm(
            "Undo this change in Cursor?",
            f"Removes the {entry.messages} message(s) ChatBridge added to chat {entry.chat_id[:8]}"
            + (" and deletes the chat it created" if entry.created else "")
            + ". Cursor must be closed.",
            "Undo",
            destructive=True,
        ):
            self._do_undo(entry)

    def _do_undo(self, entry: JournalEntry) -> None:
        self.bridge.run_async(lambda: self.sync_service.undo_cursor_write(entry.path), self._undone, self._failed)

    def _undone(self, removed: int) -> None:
        self.activity.add_event("info", f"Undid a Cursor write ({removed} message record(s) removed)")
        self.toast("Undone in Cursor")
        self.reload()

    # ------------------------------------------------------------------ auto-sync
    def _on_auto_toggled(self) -> None:
        if self.auto_var.get():
            self.autosync.start()
            self.activity.add_event("info", f"Auto-sync is on (checks every {self.settings.auto_interval}s)")
            self.toast("Auto-sync on")
        else:
            self.autosync.stop()
            self.activity.add_event("info", "Auto-sync is off")
        self._save_settings()

    def _on_carry_toggled(self) -> None:
        enabled = self.carry_var.get()
        self.settings.carry_context = enabled
        self._save_settings()
        self.toast(
            "Cursor will receive the conversation as model context on the next sync (experimental)"
            if enabled
            else "Model context is no longer carried into Cursor"
        )

    def _autosync_event(self, event: AutoSyncEvent) -> None:
        self.bridge.call(lambda: self._autosync_event_main(event))

    def _autosync_event_main(self, event: AutoSyncEvent) -> None:
        self.activity.add_event(event.kind, event.message)
        if event.kind == "synced":
            self.toast(event.message)
            self.reload()
        elif event.kind == "failed":
            self.toast(event.message)

    # ------------------------------------------------------------------ banner / polling
    def _poll_cursor(self) -> None:
        if self._closing:
            return
        if not self._poll_running:
            self._poll_running = True
            self.bridge.run_async(self._running_profiles, self._polled, lambda _e: self._polled([]))
        self.after(POLL_MS, self._poll_cursor)

    def _running_profiles(self) -> list[str]:
        return [p.label for p in self.sync_service.writable_profiles() if cursor_running(p)]

    def _polled(self, running: list[str]) -> None:
        self._poll_running = False
        self._running = running
        self._refresh_banner()

    def _refresh_banner(self) -> None:
        if self._running:
            self._show_banner(f"Cursor is open ({', '.join(self._running)}). Changes from Claude are applied to Cursor after you close it.")
        elif self._warnings:
            self._show_banner(" · ".join(self._warnings))
        else:
            self._show_banner(None)

    # ------------------------------------------------------------------ misc
    def show_profiles(self) -> ProfilesDialog:
        dialog = ProfilesDialog(self, self.settings, self._profiles_changed)
        return dialog

    def _profiles_changed(self) -> None:
        self._save_settings()
        self.reload()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.detail.set_busy(busy)
        self.update_selection_label()

    def close(self) -> None:
        self._closing = True
        self.autosync.stop()
        self.bridge.close()
        self.destroy()
        if self._on_closed is not None:
            self._on_closed()
