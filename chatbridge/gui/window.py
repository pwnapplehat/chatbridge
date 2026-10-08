"""Main window: unified Cursor + Claude conversation list, detail pane, activity log, auto-sync."""

from __future__ import annotations

import logging
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..autosync import AutoSyncer, AutoSyncEvent  # noqa: E402
from ..config import AppPaths, Settings, save_settings  # noqa: E402
from ..conversations import ConversationFilter, StateGroup, conversation_matches, projects_of_conversations  # noqa: E402
from ..cursor_writer import cursor_running  # noqa: E402
from ..sync import Conversation, JournalEntry, SyncReport, SyncService  # noqa: E402
from .activity import ActivityPage  # noqa: E402
from .detail import DetailPane  # noqa: E402
from .dialogs import ProgressDialog, ReportDialog, ReportRow, make_confirm, make_message  # noqa: E402
from .models import ConvItem  # noqa: E402
from .profiles import ProfilesDialog  # noqa: E402
from .rows import ConversationRow  # noqa: E402
from .tasks import call_on_main, run_async  # noqa: E402

LOG = logging.getLogger(__name__)
ALL_PROJECTS = "All projects"
GROUPS = list(StateGroup)
RECENT_CHOICES = [("Any time", None), ("Last 24 hours", 1), ("Last 7 days", 7), ("Last 30 days", 30)]
CSS = """
.cb-chip { padding: 2px 10px; border-radius: 99px; font-size: 0.82em; font-weight: 700; color: @window_fg_color; }
.cb-ok { background: alpha(@green_4, 0.45); }
.cb-cursor { background: alpha(@blue_3, 0.45); }
.cb-claude { background: alpha(@orange_3, 0.50); }
.cb-both { background: alpha(@purple_3, 0.45); }
.cb-bad { background: alpha(@red_3, 0.45); }
.cb-only, .cb-unknown { background: alpha(@window_fg_color, 0.12); }
.cb-pill { padding: 1px 8px; border-radius: 6px; font-size: 0.75em; border: 1px solid alpha(currentColor, 0.2); }
.cb-pill-on { background: alpha(@window_fg_color, 0.12); }
.cb-pill-off { opacity: 0.35; }
.cb-preview { font-size: 0.95em; }
"""


def report_rows(reports: list[SyncReport]) -> list[ReportRow]:
    """Convert sync reports to dialog rows."""
    rows = []
    for r in reports:
        detail = f"Claude +{r.to_claude} · Cursor +{r.to_cursor}"
        if r.detail:
            detail += f"\n{r.detail}"
        rows.append(ReportRow(r.title or r.key, r.status, detail))
    return rows


def summarize(reports: list[SyncReport], skipped: int = 0) -> str:
    """One-line summary like '2 synced, 1 deferred'."""
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.status] = counts.get(r.status, 0) + 1
    text = ", ".join(f"{n} {status}" for status, n in counts.items()) or "nothing to do"
    return text + (f", {skipped} not started (cancelled)" if skipped else "")


class MainWindow(Adw.ApplicationWindow):
    """The application window. Every user action is also a method so tests can drive it."""

    __gtype_name__ = "ChatBridgeMainWindow"

    def __init__(self, app: Adw.Application, sync: SyncService, paths: AppPaths, settings: Settings) -> None:
        super().__init__(application=app, title="ChatBridge", default_width=1280, default_height=800)
        self.sync_service, self.paths, self.settings = sync, paths, settings
        self.criteria = ConversationFilter(include_subagents=settings.show_subagents, include_empty=settings.show_empty)
        self.active_dialog: Adw.Dialog | Adw.AlertDialog | None = None
        self.last_reports: list[SyncReport] = []
        self.autosync = AutoSyncer(sync, float(settings.auto_interval), self._autosync_event)
        self.reload_count = 0
        self._busy = False
        self._preview_token = 0
        self._selected_key: str | None = None
        self._warnings: list[str] = []
        self._install_css()
        self.store = Gio.ListStore(item_type=ConvItem)
        self._build_ui()
        self.reload()
        self._cursor_poll = GLib.timeout_add_seconds(4, self._poll_cursor)
        self.connect("close-request", self._on_close)
        if settings.auto_sync:
            self.auto_switch.set_active(True)

    # ------------------------------------------------------------------ construction
    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.stack = Adw.ViewStack()
        toolbar.add_top_bar(self._build_header())
        self.banner = Adw.Banner(title="", revealed=False)
        toolbar.add_top_bar(self.banner)
        self.stack.add_titled_with_icon(self._build_conversations_page(), "conversations", "Conversations", "chat-bubble-text-symbolic")
        self.activity = ActivityPage(self.request_undo)
        self.stack.add_titled_with_icon(self.activity, "activity", "Activity", "document-open-recent-symbolic")
        toolbar.set_content(self.stack)
        self.action_bar = self._build_action_bar()
        toolbar.add_bottom_bar(self.action_bar)
        self.stack.connect(
            "notify::visible-child-name", lambda *_: self.action_bar.set_visible(self.stack.get_visible_child_name() == "conversations")
        )
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan Cursor and Claude")
        self.refresh_button.connect("clicked", lambda _b: self.reload())
        header.pack_start(self.refresh_button)
        menu = Gio.Menu()
        menu.append("Cursor backup profiles…", "win.profiles")
        menu.append("About ChatBridge", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        auto = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        auto.append(Gtk.Label(label="Auto-sync"))
        self.auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Keep linked conversations up to date automatically")
        self.auto_switch.connect("notify::active", self._on_auto_toggled)
        auto.append(self.auto_switch)
        header.pack_end(auto)
        for name, handler in (("profiles", lambda *_: self.show_profiles()), ("about", lambda *_: self._show_about())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        return header

    def _build_conversations_page(self) -> Gtk.Widget:
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=560, shrink_start_child=False, shrink_end_child=False)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left.append(self._build_filters())
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filter_model = Gtk.FilterListModel(model=self.store, filter=self.filter)
        sorter = Gtk.CustomSorter.new(
            lambda a, b, _d: (b.conv.updated_ms > a.conv.updated_ms) - (b.conv.updated_ms < a.conv.updated_ms), None
        )
        self.sort_model = Gtk.SortListModel(model=self.filter_model, sorter=sorter)
        self.selection = Gtk.SingleSelection(model=self.sort_model, autoselect=False, can_unselect=True)
        self.selection.connect("notify::selected-item", lambda *_: self._on_focus_changed())
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(ConversationRow()))
        factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        factory.connect("unbind", lambda _f, li: li.get_child().unbind())
        self.list_view = Gtk.ListView(model=self.selection, factory=factory)
        self.list_view.add_css_class("navigation-sidebar")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.list_view)
        self.list_stack = Gtk.Stack(vexpand=True)
        self.loading_page = Adw.StatusPage(title="Scanning Cursor and Claude…", description="Reading chats and sessions (read-only).")
        self.loading_page.set_child(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        self.empty_page = Adw.StatusPage(
            icon_name="edit-find-symbolic", title="No conversations match", description="Adjust the search or filters."
        )
        for name, widget in (("loading", self.loading_page), ("list", scroller), ("empty", self.empty_page)):
            self.list_stack.add_named(widget, name)
        left.append(self.list_stack)
        paned.set_start_child(left)
        self.detail = DetailPane()
        self.detail.on_compare = self.compare_focused
        self.detail.on_sync = self.sync_focused
        self.detail.on_unlink = self.unlink_focused
        paned.set_end_child(self.detail)
        return paned

    def _build_filters(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=8, margin_start=12, margin_end=12)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search title, first message, project…")
        self.search_entry.connect("search-changed", lambda _e: self._update_criteria())
        box.append(self.search_entry)
        row = Gtk.Box(spacing=8)
        self.group_drop = Gtk.DropDown.new_from_strings([g.value for g in GROUPS])
        self.project_model = Gtk.StringList.new([ALL_PROJECTS])
        self.project_drop = Gtk.DropDown(model=self.project_model, hexpand=True, enable_search=True)
        self.recent_drop = Gtk.DropDown.new_from_strings([label for label, _ in RECENT_CHOICES])
        for drop in (self.group_drop, self.project_drop, self.recent_drop):
            drop.connect("notify::selected", lambda *_: self._update_criteria())
            row.append(drop)
        box.append(row)
        toggles = Gtk.Box(spacing=18)
        self.subagent_check = Gtk.CheckButton(label="Subagent chats", active=self.settings.show_subagents)
        self.empty_check = Gtk.CheckButton(label="Empty drafts", active=self.settings.show_empty)
        for check in (self.subagent_check, self.empty_check):
            check.connect("toggled", lambda _c: self._update_criteria())
            toggles.append(check)
        box.append(toggles)
        return box

    def _build_action_bar(self) -> Gtk.ActionBar:
        bar = Gtk.ActionBar()
        self.selected_label = Gtk.Label(label="0 selected")
        select_all = Gtk.Button(label="Select all shown")
        select_all.connect("clicked", lambda _b: self.select_all_shown())
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda _b: self.clear_selection())
        for widget in (self.selected_label, select_all, clear):
            bar.pack_start(widget)
        self.sync_selected_button = Gtk.Button(label="Sync selected…")
        self.sync_selected_button.add_css_class("suggested-action")
        self.sync_selected_button.connect("clicked", lambda _b: self.sync_selected())
        self.compare_selected_button = Gtk.Button(label="Compare selected")
        self.compare_selected_button.connect("clicked", lambda _b: self.compare_selected())
        bar.pack_end(self.sync_selected_button)
        bar.pack_end(self.compare_selected_button)
        return bar

    # ------------------------------------------------------------------ catalog
    def reload(self) -> None:
        """Rescan both tools in the background."""
        self.list_stack.set_visible_child_name("loading")
        self.refresh_button.set_sensitive(False)
        run_async(self.sync_service.load_conversations, self._loaded, self._load_failed)

    def _loaded(self, result: tuple[list[Conversation], list[str]]) -> None:
        conversations, warnings = result
        self.reload_count += 1
        ticked = {i.conv.key for i in self.all_items() if i.selected}
        self.store.remove_all()
        for conv in conversations:
            item = ConvItem(conv)
            item.selected = conv.key in ticked and item.selectable
            item.connect("notify::selected", lambda *_: self.update_selection_label())
            self.store.append(item)
        names = projects_of_conversations(conversations)
        self.project_model.splice(0, self.project_model.get_n_items(), [ALL_PROJECTS, *names])
        self.project_drop.set_selected(0)
        self._warnings = warnings
        self.refresh_button.set_sensitive(True)
        self.detail.set_profiles(self.sync_service.writable_profiles())
        self._update_criteria()
        self.refresh_journals()
        self._poll_cursor()
        self._restore_focus()

    def _load_failed(self, exc: BaseException) -> None:
        self.refresh_button.set_sensitive(True)
        self.list_stack.set_visible_child_name("empty")
        self.empty_page.set_title("Could not read Cursor / Claude data")
        self.empty_page.set_description(f"{type(exc).__name__}: {exc}")

    def all_items(self) -> list[ConvItem]:
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def shown_items(self) -> list[ConvItem]:
        return [self.sort_model.get_item(i) for i in range(self.sort_model.get_n_items())]

    def selected_items(self) -> list[ConvItem]:
        return [i for i in self.all_items() if i.selected]

    def focused_item(self) -> ConvItem | None:
        return self.selection.get_selected_item()

    def _restore_focus(self) -> None:
        if self._selected_key is None:
            return
        for index, item in enumerate(self.shown_items()):
            if item.conv.key == self._selected_key:
                self.selection.set_selected(index)
                return

    # ------------------------------------------------------------------ filtering
    def _filter_func(self, item: ConvItem) -> bool:
        return conversation_matches(item.conv, self.criteria)

    def _update_criteria(self) -> None:
        index = self.project_drop.get_selected()
        project = None if index == 0 or index >= self.project_model.get_n_items() else self.project_model.get_string(index)
        days = RECENT_CHOICES[self.recent_drop.get_selected()][1]
        self.criteria = ConversationFilter(
            query=self.search_entry.get_text(),
            project=project,
            group=GROUPS[self.group_drop.get_selected()],
            include_subagents=self.subagent_check.get_active(),
            include_empty=self.empty_check.get_active(),
            updated_since_ms=None if days is None else int((time.time() - days * 86400) * 1000),
        )
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self.list_stack.set_visible_child_name("list" if self.sort_model.get_n_items() else "empty")
        self._save_settings()
        self.update_selection_label()

    def _save_settings(self) -> None:
        self.settings.show_subagents = self.subagent_check.get_active()
        self.settings.show_empty = self.empty_check.get_active()
        self.settings.auto_sync = self.auto_switch.get_active() if hasattr(self, "auto_switch") else self.settings.auto_sync
        try:
            save_settings(self.paths, self.settings)
        except OSError:
            LOG.exception("could not save settings")

    # ------------------------------------------------------------------ selection
    def select_all_shown(self) -> None:
        for item in self.shown_items():
            if item.selectable:
                item.selected = True
        self.update_selection_label()

    def clear_selection(self) -> None:
        for item in self.all_items():
            item.selected = False
        self.update_selection_label()

    def update_selection_label(self) -> None:
        chosen = self.selected_items()
        hidden = sum(1 for i in chosen if not conversation_matches(i.conv, self.criteria))
        self.selected_label.set_text(f"{len(chosen)} selected" + (f" ({hidden} hidden by filters)" if hidden else ""))
        enabled = bool(chosen) and not self._busy
        self.sync_selected_button.set_sensitive(enabled)
        self.compare_selected_button.set_sensitive(enabled)

    # ------------------------------------------------------------------ focus / detail
    def _on_focus_changed(self) -> None:
        item = self.focused_item()
        self._preview_token += 1
        if item is None:
            self.detail.show_empty()
            return
        self._selected_key = item.conv.key
        self.detail.show_conversation(item.conv)
        conv, token = item.conv, self._preview_token
        run_async(lambda: self.sync_service.preview(conv), lambda lines: self._preview_ready(token, lines), lambda exc: None)

    def _preview_ready(self, token: int, lines: list) -> None:  # type: ignore[type-arg]
        if token == self._preview_token:
            self.detail.set_preview(lines)

    # ------------------------------------------------------------------ single-conversation actions
    def compare_focused(self) -> None:
        item = self.focused_item()
        if item is None or self._busy:
            return
        conv = item.conv
        self._set_busy(True)
        self.detail.set_result("Comparing…")
        run_async(
            lambda: self.sync_service.sync(conv, "both", apply=False, target_profile=self.detail.selected_profile()),
            self._compared,
            self._failed,
        )

    def _compared(self, report: SyncReport) -> None:
        self._set_busy(False)
        self.detail.set_result(self.describe(report))

    @staticmethod
    def describe(report: SyncReport) -> str:
        if report.status == "failed":
            return f"Could not compare: {report.detail}"
        if report.to_claude == 0 and report.to_cursor == 0:
            return "Nothing to sync: both sides already have the same messages."
        parts = []
        if report.to_claude:
            parts.append(f"{report.to_claude} message(s) will be added to Claude")
        if report.to_cursor:
            parts.append(f"{report.to_cursor} message(s) will be added to Cursor")
        return " and ".join(parts) + "."

    def sync_focused(self) -> None:
        """Dry-run first so the confirmation can say exactly what will happen."""
        item = self.focused_item()
        if item is None or self._busy:
            return
        conv = item.conv
        profile = self.detail.selected_profile()
        self._set_busy(True)
        run_async(
            lambda: self.sync_service.sync(conv, "both", apply=False, target_profile=profile),
            lambda report: self._confirm_single(conv, profile, report),
            self._failed,
        )

    def _confirm_single(self, conv: Conversation, profile: object, report: SyncReport) -> None:
        self._set_busy(False)
        if report.status == "failed":
            self._present(make_message("Cannot sync this conversation", report.detail))
            return
        if report.to_claude == 0 and report.to_cursor == 0:
            self.toast("Already in sync")
            self.detail.set_result(self.describe(report))
            return
        notes = []
        if report.to_cursor:
            notes.append("Cursor must be closed on the target profile; if it is open the Cursor part is deferred, not lost.")
        if report.to_claude:
            notes.append("Claude: reopen the session (or restart the app) to see the new messages.")
        dialog = make_confirm(f"Sync “{conv.title or 'conversation'}”?", self.describe(report) + "\n\n" + "\n".join(notes), "Sync")
        dialog.connect("response", lambda _d, r: self._apply_single(conv, profile) if r == "confirm" else None)
        self._present(dialog)

    def _apply_single(self, conv: Conversation, profile: object) -> None:
        self._set_busy(True)
        self.detail.set_result("Syncing…")
        run_async(lambda: self.sync_service.sync(conv, "both", apply=True, target_profile=profile), self._single_done, self._failed)  # type: ignore[arg-type]

    def _single_done(self, report: SyncReport) -> None:
        self._set_busy(False)
        self.last_reports = [report]
        self.activity.add_event(
            "failed" if report.status == "failed" else report.status if report.status in ("synced", "deferred") else "info",
            f"{report.title}: {self.describe(report) if report.status != 'deferred' else report.detail}",
        )
        self.detail.set_result(report.detail if report.status in ("failed", "deferred") else f"Synced. {self.describe(report)}")
        if report.status == "failed":
            self._present(make_message("Sync failed", report.detail))
        else:
            self.toast("Synced" if report.status == "synced" else "Waiting: " + report.detail)
        self.reload()

    def unlink_focused(self) -> None:
        item = self.focused_item()
        if item is None:
            return
        conv = item.conv
        dialog = make_confirm(
            "Stop syncing this conversation?",
            "The pairing is forgotten. The Cursor chat and the Claude session both stay exactly as they are.",
            "Stop syncing",
            destructive=True,
        )
        dialog.connect("response", lambda _d, r: self._do_unlink(conv) if r == "confirm" else None)
        self._present(dialog)

    def _do_unlink(self, conv: Conversation) -> None:
        self.sync_service.unlink(conv)
        self.toast("Pairing removed")
        self.reload()

    # ------------------------------------------------------------------ bulk actions
    def compare_selected(self) -> None:
        self._run_bulk(apply=False)

    def sync_selected(self) -> None:
        items = self.selected_items()
        if not items or self._busy:
            return
        dialog = make_confirm(
            f"Sync {len(items)} conversation{'s' if len(items) != 1 else ''}?",
            "Each conversation gets the messages the other tool is missing. Conversations that exist in only one tool are created in the other. "
            "Nothing is overwritten or deleted. Use “Compare selected” first to preview.",
            "Sync",
        )
        dialog.connect("response", lambda _d, r: self._run_bulk(apply=True) if r == "confirm" else None)
        self._present(dialog)

    def _run_bulk(self, apply: bool) -> None:
        items = self.selected_items()
        if not items or self._busy:
            return
        convs = [i.conv for i in items]
        cancel = threading.Event()
        progress = ProgressDialog("Syncing" if apply else "Comparing", cancel.set)
        self._set_busy(True)
        self._present(progress)
        titles = {c.key: c.title for c in convs}
        profile = self.detail.selected_profile()
        run_async(
            lambda: self.sync_service.sync_many(
                convs,
                "both",
                apply,
                lambda p: call_on_main(lambda: progress.update(p.done, p.total, titles.get(p.report.key, p.report.title))),
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
        self._present(ReportDialog("Sync finished" if apply else "Comparison", summary, report_rows(reports)))
        if apply:
            self.activity.add_event("synced", f"Manual sync: {summary.splitlines()[0]}")
            self.reload()

    def _failed(self, exc: BaseException, progress: ProgressDialog | None = None) -> None:
        if progress is not None:
            progress.finish()
        self._set_busy(False)
        self._present(make_message("Something went wrong", f"{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------------ undo (Cursor journals)
    def refresh_journals(self) -> None:
        self.activity.set_journals(self.sync_service.list_journals())

    def request_undo(self, entry: JournalEntry) -> None:
        dialog = make_confirm(
            "Undo this change in Cursor?",
            f"Removes the {entry.messages} message(s) ChatBridge added to chat {entry.chat_id[:8]}"
            + (" and deletes the chat it created" if entry.created else "")
            + ". Cursor must be closed.",
            "Undo",
            destructive=True,
        )
        dialog.connect("response", lambda _d, r: self._do_undo(entry) if r == "confirm" else None)
        self._present(dialog)

    def _do_undo(self, entry: JournalEntry) -> None:
        run_async(lambda: self.sync_service.undo_cursor_write(entry.path), lambda n: self._undone(n), self._failed)

    def _undone(self, removed: int) -> None:
        self.activity.add_event("info", f"Undid a Cursor write ({removed} message record(s) removed)")
        self.toast("Undone in Cursor")
        self.reload()

    # ------------------------------------------------------------------ auto-sync
    def _on_auto_toggled(self, switch: Gtk.Switch, _pspec: object) -> None:
        if switch.get_active():
            self.autosync.start()
            self.activity.add_event("info", f"Auto-sync is on (checks every {self.settings.auto_interval}s)")
            self.toast("Auto-sync on")
        else:
            self.autosync.stop()
            self.activity.add_event("info", "Auto-sync is off")
        self._save_settings()

    def _autosync_event(self, event: AutoSyncEvent) -> None:
        call_on_main(lambda: self._autosync_event_main(event))

    def _autosync_event_main(self, event: AutoSyncEvent) -> None:
        self.activity.add_event(event.kind, event.message)
        if event.kind == "synced":
            self.toast(event.message)
            self.reload()
        elif event.kind == "failed":
            self.toast(event.message)

    # ------------------------------------------------------------------ banner / polling
    def _poll_cursor(self) -> bool:
        running = [p.label for p in self.sync_service.writable_profiles() if cursor_running(p)]
        if running:
            self.banner.set_title(f"Cursor is open ({', '.join(running)}). Changes from Claude are applied to Cursor after you close it.")
            self.banner.set_revealed(True)
        elif self._warnings:
            self.banner.set_title(" · ".join(self._warnings))
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
        return True

    # ------------------------------------------------------------------ misc
    def show_profiles(self) -> None:
        self._present(ProfilesDialog(self, self.settings, self._profiles_changed))

    def _profiles_changed(self) -> None:
        self._save_settings()
        self.reload()

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="ChatBridge",
            version="1.0.0",
            comments="Two-way sync of chat history between Cursor and Claude. Messages, reasoning and tool calls, appended safely and reversibly.",
            developer_name="ChatBridge contributors",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)

    def _present(self, dialog: Adw.Dialog | Adw.AlertDialog) -> None:
        self.active_dialog = dialog
        dialog.connect("closed", self._on_dialog_closed)
        dialog.present(self)

    def _on_dialog_closed(self, dialog: Adw.Dialog | Adw.AlertDialog) -> None:
        """Forget a dialog only if it is still the active one (a response handler may already have opened the next)."""
        if self.active_dialog is dialog:
            self.active_dialog = None

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.detail.set_busy(busy)
        self.update_selection_label()

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    def _on_close(self, _window: Gtk.Window) -> bool:
        GLib.source_remove(self._cursor_poll)
        self.autosync.stop()
        return False
