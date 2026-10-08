"""End-to-end GUI tests: the real window, real buttons and real dialogs, on synthetic data.

Run under a display, e.g.:  xvfb-run -a .venv/bin/pytest tests/test_gui_e2e.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from chatbridge import cursor_writer  # noqa: E402
from chatbridge.config import Settings, load_settings  # noqa: E402
from chatbridge.gui.app import ImporterApp  # noqa: E402
from chatbridge.gui.dialogs import ProgressDialog, ReportDialog  # noqa: E402
from chatbridge.gui.models import ConvItem  # noqa: E402
from chatbridge.gui.profiles import ProfilesDialog  # noqa: E402
from chatbridge.gui.window import MainWindow  # noqa: E402
from chatbridge.sync import SyncService, SyncState  # noqa: E402
from chatbridge.writer import local_session_name, session_uuid  # noqa: E402
from tests.fixtures import (  # noqa: E402
    CHAT_BACKUP_ONLY,
    CHAT_MAIN,
    CHAT_SUB,
    CHAT_TRANSCRIPT,
    CLAUDE_CLI,
    CLAUDE_KEY,
    World,
    claude_follow_up,
)
from tests.test_sync import native_session  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")), reason="needs a display (use xvfb-run)"
)


def pump(condition: Callable[[], bool], timeout: float = 20.0) -> bool:
    """Run the GTK main loop until condition() is true."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if condition():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture(scope="module")
def gtk_app() -> Iterator[ImporterApp]:
    app = ImporterApp(application_id="io.github.pwnapplehat.ChatBridgeTest", non_unique=True)
    app.register(None)
    yield app


@pytest.fixture
def window(gtk_app: ImporterApp, world: World) -> Iterator[MainWindow]:
    native_session(world)
    settings = Settings(extra_profiles=[("backup", str(world.backup_user))])
    win = MainWindow(gtk_app, SyncService(world.paths, settings), world.paths, settings)
    win.present()
    assert pump(lambda: win.store.get_n_items() == 6), "conversation list never loaded"
    yield win
    win.autosync.stop()
    win.destroy()


def item_by(win: MainWindow, *, cursor_id: str | None = None, claude_key: str | None = None) -> ConvItem:
    for item in win.all_items():
        if cursor_id and item.conv.cursor and item.conv.cursor.ref.chat_id == cursor_id:
            return item
        if claude_key and item.conv.claude and item.conv.claude.key == claude_key:
            return item
    raise AssertionError("conversation not in the list")


def focus(win: MainWindow, item: ConvItem) -> None:
    win.search_entry.set_text("")
    assert pump(lambda: any(i is item for i in win.shown_items()))
    win.selection.set_selected(next(n for n, i in enumerate(win.shown_items()) if i is item))
    assert pump(lambda: win.detail.title.get_text() == (item.conv.title or "(untitled)"))


def answer(win: MainWindow, response: str) -> None:
    """Click a button of the currently shown alert dialog."""
    assert pump(lambda: isinstance(win.active_dialog, Adw.AlertDialog)), "no alert dialog appeared"
    dialog = win.active_dialog
    assert dialog is not None
    dialog.emit("response", response)
    dialog.force_close()  # Adwaita closes an alert right after a response


def wait_report(win: MainWindow) -> ReportDialog:
    assert pump(lambda: isinstance(win.active_dialog, ReportDialog)), "no report dialog appeared"
    assert isinstance(win.active_dialog, ReportDialog)
    return win.active_dialog


def reload_and_wait(win: MainWindow) -> None:
    before = win.reload_count
    win.reload()
    assert pump(lambda: win.reload_count > before), "reload did not finish"


def logs(world: World) -> list[Path]:
    """Claude logs created by ChatBridge (the native fixture session is excluded)."""
    return [p for p in (world.paths.claude_dir / "projects").glob("*/*.jsonl") if p.stem != CLAUDE_CLI]


def cursor_chat_count(world: World) -> int:
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    (count,) = conn.execute("SELECT count(*) FROM composerHeaders").fetchone()
    conn.close()
    return int(count)


# --------------------------------------------------------------------------- list, filters, detail
@pytest.mark.gui
def test_startup_lists_both_tools_with_states(window: MainWindow) -> None:
    shown = {i.conv.title for i in window.shown_items()}
    assert {"Fix the parser", "Old windows chat", "Refactor lexer", "transcript question"} <= shown
    assert all(i.conv.cursor is None or not i.conv.cursor.ref.is_subagent for i in window.shown_items()), "subagents hidden by default"
    assert item_by(window, claude_key=CLAUDE_KEY).conv.state is SyncState.CLAUDE_ONLY
    assert item_by(window, cursor_id=CHAT_MAIN).conv.state is SyncState.CURSOR_ONLY
    assert not window.auto_switch.get_active()
    assert window.sync_selected_button.get_sensitive() is False, "nothing is selected by default"
    assert window.selected_label.get_text() == "0 selected"


@pytest.mark.gui
def test_filters_search_group_and_toggles(window: MainWindow) -> None:
    window.search_entry.set_text("legacy")
    assert pump(lambda: [i.conv.title for i in window.shown_items()] == ["Old windows chat"])
    window.search_entry.set_text("")
    window.group_drop.set_selected(4)  # Only in Claude
    assert pump(lambda: [i.conv.title for i in window.shown_items()] == ["Refactor lexer"])
    window.group_drop.set_selected(0)
    window.subagent_check.set_active(True)
    assert pump(lambda: item_by(window, cursor_id=CHAT_SUB) in window.shown_items())
    window.search_entry.set_text("zzz-nothing-matches")
    assert pump(lambda: window.shown_items() == [])
    assert window.list_stack.get_visible_child_name() == "empty"


@pytest.mark.gui
def test_detail_pane_adapts_to_each_kind_of_conversation(window: MainWindow) -> None:
    focus(window, item_by(window, cursor_id=CHAT_MAIN))
    assert window.detail.sync_button.get_label() == "Import into Claude" and not window.detail.compare_button.get_visible()
    buffer = window.detail.buffer
    assert pump(lambda: "Please fix the parser bug" in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))
    focus(window, item_by(window, claude_key=CLAUDE_KEY))
    assert window.detail.sync_button.get_label() == "Send to Cursor" and window.detail.target_row.get_visible()
    assert pump(lambda: "Refactor the lexer please" in buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))


# --------------------------------------------------------------------------- single sync, both directions
@pytest.mark.gui
def test_import_cursor_chat_into_claude_through_the_ui(window: MainWindow, world: World) -> None:
    item = item_by(window, cursor_id=CHAT_MAIN)
    focus(window, item)
    window.detail.sync_button.emit("clicked")
    answer(window, "cancel")
    assert logs(world) == [], "cancelling the confirmation must write nothing"
    window.detail.sync_button.emit("clicked")
    answer(window, "confirm")
    assert pump(lambda: item_by(window, cursor_id=CHAT_MAIN).conv.state is SyncState.IN_SYNC and bool(window.last_reports)), (
        "never became In sync"
    )
    assert session_uuid(CHAT_MAIN) in {p.stem for p in logs(world)}
    assert (world.paths.desktop_dir / "org" / "acct" / f"{local_session_name(CHAT_MAIN)}.json").exists()


@pytest.mark.gui
def test_send_claude_session_to_cursor_then_undo_from_activity(window: MainWindow, world: World) -> None:
    before = cursor_chat_count(world)
    item = item_by(window, claude_key=CLAUDE_KEY)
    focus(window, item)
    window.detail.sync_button.emit("clicked")
    answer(window, "confirm")
    assert pump(lambda: item_by(window, claude_key=CLAUDE_KEY).conv.state is SyncState.IN_SYNC and bool(window.last_reports))
    assert cursor_chat_count(world) == before + 1
    journals = window.sync_service.list_journals()
    assert len(journals) == 1 and journals[0].created and journals[0].messages == 6
    assert pump(lambda: len(window.activity.journal_rows) == 1 and "Created chat" in window.activity.journal_rows[0].get_title())

    window.request_undo(journals[0])
    answer(window, "confirm")
    assert pump(lambda: cursor_chat_count(world) == before and item_by(window, claude_key=CLAUDE_KEY).conv.state is SyncState.CLAUDE_ONLY)
    assert window.sync_service.list_journals() == []
    assert (world.paths.desktop_dir / "org" / "acct" / f"{CLAUDE_KEY}.json").exists(), "the Claude session itself must be untouched"


@pytest.mark.gui
def test_follow_up_in_claude_is_compared_and_synced_to_cursor(window: MainWindow, world: World) -> None:
    window.sync_service.sync(item_by(window, claude_key=CLAUDE_KEY).conv, apply=True)
    reload_and_wait(window)
    claude_follow_up(world, CLAUDE_CLI, [("u", "add docs"), ("a", "docs added")])
    reload_and_wait(window)
    item = item_by(window, claude_key=CLAUDE_KEY)
    assert item.conv.state is SyncState.CLAUDE_CHANGED
    focus(window, item)
    window.detail.compare_button.emit("clicked")
    assert pump(lambda: "2 message(s) will be added to Cursor" in window.detail.result.get_text())
    window.detail.sync_button.emit("clicked")
    answer(window, "confirm")
    assert pump(lambda: item_by(window, claude_key=CLAUDE_KEY).conv.state is SyncState.IN_SYNC and bool(window.last_reports))
    assert window.last_reports[0].status == "synced" and window.last_reports[0].to_cursor == 2


@pytest.mark.gui
def test_unlink_forgets_the_pairing_only(window: MainWindow, world: World) -> None:
    window.sync_service.sync(item_by(window, cursor_id=CHAT_MAIN).conv, apply=True)
    reload_and_wait(window)
    item = item_by(window, cursor_id=CHAT_MAIN)
    focus(window, item)
    assert window.detail.unlink_button.get_visible()
    window.detail.unlink_button.emit("clicked")
    answer(window, "confirm")
    assert pump(lambda: window.sync_service.links.all() == [])
    assert len(logs(world)) == 1, "unlinking must not delete the Claude session"


# --------------------------------------------------------------------------- bulk
@pytest.mark.gui
def test_bulk_compare_then_sync_selected(window: MainWindow, world: World) -> None:
    window.search_entry.set_text("")
    assert pump(lambda: len(window.shown_items()) >= 4)
    window.select_all_shown()
    count = len(window.selected_items())
    assert count >= 4 and window.selected_label.get_text() == f"{count} selected"
    chats_before = cursor_chat_count(world)
    window.compare_selected_button.emit("clicked")
    report = wait_report(window)
    assert "dry-run" in report.summary_label.get_text()
    assert logs(world) == [] and cursor_chat_count(world) == chats_before, "a comparison must not write anything"
    report.close()
    window.sync_selected_button.emit("clicked")
    answer(window, "confirm")
    report = wait_report(window)
    assert "synced" in report.summary_label.get_text()
    assert {p.stem for p in logs(world)} >= {session_uuid(CHAT_MAIN), session_uuid(CHAT_BACKUP_ONLY), session_uuid(CHAT_TRANSCRIPT)}
    assert pump(lambda: any(i.conv.state is SyncState.IN_SYNC for i in window.all_items())), "list never refreshed after the sync"


@pytest.mark.gui
def test_bulk_cancel_confirmation_writes_nothing(window: MainWindow, world: World) -> None:
    window.select_all_shown()
    window.sync_selected_button.emit("clicked")
    answer(window, "cancel")
    pump(lambda: False, timeout=0.5)  # give any (wrongly) started work a chance to run
    assert logs(world) == [] and window.sync_service.links.all() == [], "cancelling the confirmation must write nothing"


# --------------------------------------------------------------------------- safety UX
@pytest.mark.gui
def test_cursor_open_shows_banner_and_defers_cursor_writes(window: MainWindow, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    window.sync_service.sync(item_by(window, claude_key=CLAUDE_KEY).conv, apply=True)
    reload_and_wait(window)
    claude_follow_up(world, CLAUDE_CLI, [("u", "while cursor is open"), ("a", "noted")])
    reload_and_wait(window)
    monkeypatch.setattr("chatbridge.gui.window.cursor_running", lambda profile: True)
    monkeypatch.setattr(cursor_writer, "cursor_running", lambda profile: True)
    window._poll_cursor()
    assert window.banner.get_revealed() and "Cursor is open" in window.banner.get_title()
    item = item_by(window, claude_key=CLAUDE_KEY)
    focus(window, item)
    window.detail.sync_button.emit("clicked")
    answer(window, "confirm")
    assert pump(lambda: bool(window.last_reports))
    assert window.last_reports[0].status == "deferred" and "Close Cursor" in window.last_reports[0].detail
    assert item_by(window, claude_key=CLAUDE_KEY).conv.state is SyncState.CLAUDE_CHANGED or pump(
        lambda: item_by(window, claude_key=CLAUDE_KEY).conv.state is SyncState.CLAUDE_CHANGED
    )
    monkeypatch.undo()
    window._poll_cursor()
    assert not window.banner.get_revealed()


@pytest.mark.gui
def test_failed_conversation_is_reported_not_fatal(window: MainWindow, world: World) -> None:
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    conn.execute("UPDATE cursorDiskKV SET value = 'broken' WHERE key LIKE ?", (f"bubbleId:{CHAT_MAIN}:%",))
    conn.commit()
    conn.close()
    item_by(window, cursor_id=CHAT_MAIN).selected = True
    item_by(window, cursor_id=CHAT_BACKUP_ONLY).selected = True
    window.sync_selected_button.emit("clicked")
    answer(window, "confirm")
    report = wait_report(window)
    assert "1 synced" in report.summary_label.get_text() and "1 failed" in report.summary_label.get_text()
    assert [p.stem for p in logs(world)] == [session_uuid(CHAT_BACKUP_ONLY)]
    assert not isinstance(window.active_dialog, ProgressDialog)


# --------------------------------------------------------------------------- auto-sync, settings, profiles
@pytest.mark.gui
def test_auto_sync_keeps_a_linked_conversation_up_to_date(window: MainWindow, world: World) -> None:
    window.sync_service.sync(item_by(window, claude_key=CLAUDE_KEY).conv, apply=True)
    reload_and_wait(window)
    window.autosync.interval = 0.2
    window.auto_switch.set_active(True)
    assert window.autosync.running and load_settings(world.paths).auto_sync is True
    claude_follow_up(world, CLAUDE_CLI, [("u", "typed while auto-sync runs"), ("a", "answer")])
    assert pump(lambda: any("Synced 1 conversation" in row.get_title() for row in window.activity.log_rows), timeout=30), (
        "auto-sync never reported"
    )
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    texts = [json.loads(v).get("text") for (v,) in conn.execute("SELECT value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'")]
    conn.close()
    assert "typed while auto-sync runs" in texts
    window.auto_switch.set_active(False)
    assert pump(lambda: not window.autosync.running) and load_settings(world.paths).auto_sync is False


@pytest.mark.gui
def test_auto_sync_never_imports_unlinked_conversations(window: MainWindow, world: World) -> None:
    window.autosync.interval = 0.2
    window.auto_switch.set_active(True)
    time.sleep(1.0)
    pump(lambda: False, timeout=0.5)
    window.auto_switch.set_active(False)
    assert logs(world) == [] and window.sync_service.links.all() == []


@pytest.mark.gui
def test_settings_persist(window: MainWindow, world: World) -> None:
    window.empty_check.set_active(True)
    window.subagent_check.set_active(True)
    saved = load_settings(world.paths)
    assert saved.show_empty and saved.show_subagents
    assert saved.extra_profiles == [("backup", str(world.backup_user))]


@pytest.mark.gui
def test_profiles_dialog_validates_and_persists(window: MainWindow, world: World, tmp_path: Path) -> None:
    window.show_profiles()
    dialog = window.active_dialog
    assert isinstance(dialog, ProfilesDialog)
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    assert dialog.add_path(plain) is False and "state.vscdb" in dialog.error_label.get_text()
    assert dialog.add_path(world.backup_user) is False and "already" in dialog.error_label.get_text()
    assert window.settings.extra_profiles == [("backup", str(world.backup_user))]


@pytest.mark.gui
def test_activity_page_is_reachable(window: MainWindow) -> None:
    window.stack.set_visible_child_name("activity")
    assert window.stack.get_visible_child_name() == "activity"
    assert window.activity.journal_rows, "the empty-state row must be present"
