"""End-to-end tests of the Tk front end (the Windows GUI): the real window and real widgets, on synthetic data.

Confirmation / message dialogs are modal, so the window routes them through two methods (`confirm`, `message`) that the
tests replace; everything else (lists, filters, buttons, threads, dialogs that are not modal) is the real thing.
"""

from __future__ import annotations

import json
import sqlite3
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from chatbridge import cursor_writer
from chatbridge.config import Settings, load_settings
from chatbridge.sync import Conversation, SyncService, SyncState
from chatbridge.tkgui.dialogs import ProfilesDialog, ReportDialog, ask_confirm
from chatbridge.tkgui.window import MainWindow
from chatbridge.writer import local_session_name, session_uuid
from tests.fixtures import (
    CHAT_BACKUP_ONLY,
    CHAT_MAIN,
    CHAT_SUB,
    CHAT_TRANSCRIPT,
    CLAUDE_CLI,
    CLAUDE_KEY,
    World,
    claude_follow_up,
)
from tests.test_sync import native_session


def _display_available() -> bool:
    try:
        import subprocess
        import sys

        code = "import tkinter; tkinter.Tk().destroy()"
        return subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60).returncode == 0
    except (ImportError, OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _display_available(), reason="needs a display (use xvfb-run on Linux)")


class Answers:
    """Scripted replies to the modal confirmation dialogs, plus a log of what was asked."""

    def __init__(self) -> None:
        self.queue: list[bool] = []
        self.asked: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str]] = []

    def confirm(self, heading: str, body: str, label: str, destructive: bool = False) -> bool:
        self.asked.append((heading, body))
        assert self.queue, f"unexpected confirmation: {heading}"
        return self.queue.pop(0)

    def message(self, heading: str, body: str) -> None:
        self.messages.append((heading, body))


def pump(win: tk.Misc, condition: Callable[[], bool], timeout: float = 20.0) -> bool:
    """Run the Tk event loop until condition() is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        win.update()
        if condition():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def answers() -> Answers:
    return Answers()


@pytest.fixture(scope="module")
def root() -> Iterator[tk.Tk]:
    """One hidden Tk root for the whole module (creating and destroying many roots in one process is unreliable)."""
    tk_root = tk.Tk()
    tk_root.withdraw()
    yield tk_root
    tk_root.destroy()


@pytest.fixture
def window(root: tk.Tk, world: World, answers: Answers) -> Iterator[MainWindow]:
    native_session(world)
    settings = Settings(extra_profiles=[("backup", str(world.backup_user))])
    win = MainWindow(root, SyncService(world.paths, settings), world.paths, settings)
    win.confirm = answers.confirm  # type: ignore[method-assign]
    win.message = answers.message  # type: ignore[method-assign]
    assert pump(win, lambda: len(win.conversations) == 6), "conversation list never loaded"
    yield win
    win.close()


def conv_by(win: MainWindow, *, cursor_id: str | None = None, claude_key: str | None = None) -> Conversation:
    for conv in win.all_items():
        if cursor_id and conv.cursor and conv.cursor.ref.chat_id == cursor_id:
            return conv
        if claude_key and conv.claude and conv.claude.key == claude_key:
            return conv
    raise AssertionError("conversation not in the list")


def focus(win: MainWindow, conv: Conversation) -> None:
    win.search_var.set("")
    win.focus_conversation(conv)
    assert pump(win, lambda: win.detail.title.cget("text") == (conv.title or "(untitled)"))


def reload_and_wait(win: MainWindow) -> None:
    before = win.reload_count
    win.reload()
    assert pump(win, lambda: win.reload_count > before), "reload did not finish"


def preview_text(win: MainWindow) -> str:
    return str(win.detail.preview.get("1.0", "end"))


def logs(world: World) -> list[Path]:
    """Claude logs created by ChatBridge (the native fixture session is excluded)."""
    return [p for p in (world.paths.claude_dir / "projects").glob("*/*.jsonl") if p.stem != CLAUDE_CLI]


def cursor_chat_count(world: World) -> int:
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    (count,) = conn.execute("SELECT count(*) FROM composerHeaders").fetchone()
    conn.close()
    return int(count)


# --------------------------------------------------------------------------- list, filters, detail
def test_startup_lists_both_tools_with_states(window: MainWindow) -> None:
    shown = {c.title for c in window.shown_items()}
    assert {"Fix the parser", "Old windows chat", "Refactor lexer", "transcript question"} <= shown
    assert all(c.cursor is None or not c.cursor.ref.is_subagent for c in window.shown_items()), "subagents hidden by default"
    assert conv_by(window, claude_key=CLAUDE_KEY).state is SyncState.CLAUDE_ONLY
    assert conv_by(window, cursor_id=CHAT_MAIN).state is SyncState.CURSOR_ONLY
    assert not window.auto_var.get()
    assert window.sync_selected_button.instate(["disabled"]), "nothing is selected by default"
    assert window.selected_label.cget("text") == "0 selected"
    assert window.tree.exists(conv_by(window, cursor_id=CHAT_MAIN).key)


def test_filters_search_group_and_toggles(window: MainWindow) -> None:
    window.search_var.set("legacy")
    assert [c.title for c in window.shown_items()] == ["Old windows chat"]
    window.search_var.set("")
    window.group_var.set("Only in Claude")
    window._update_criteria()
    assert [c.title for c in window.shown_items()] == ["Refactor lexer"]
    window.group_var.set("All conversations")
    window.subagent_var.set(True)
    window._update_criteria()
    assert conv_by(window, cursor_id=CHAT_SUB) in window.shown_items()
    window.search_var.set("zzz-nothing-matches")
    assert window.shown_items() == [] and "0 of" in window.status_label.cget("text")
    window.search_var.set("")
    window.project_var.set("parser-app")
    window._update_criteria()
    assert {c.title for c in window.shown_items()} >= {"Fix the parser"}
    assert all(c.project == "parser-app" for c in window.shown_items())


def test_detail_pane_adapts_to_each_kind_of_conversation(window: MainWindow) -> None:
    focus(window, conv_by(window, cursor_id=CHAT_MAIN))
    assert window.detail.sync_button.cget("text") == "Import into Claude" and not window.detail.compare_button.winfo_ismapped()
    assert pump(window, lambda: "Please fix the parser bug" in preview_text(window))
    focus(window, conv_by(window, claude_key=CLAUDE_KEY))
    assert window.detail.sync_button.cget("text") == "Send to Cursor" and window.detail.target_frame.winfo_ismapped()
    assert pump(window, lambda: "Refactor the lexer please" in preview_text(window))


def test_ticking_rows_and_the_header_select_all(window: MainWindow) -> None:
    conv = conv_by(window, cursor_id=CHAT_MAIN)
    window._toggle(conv)
    assert window.selected_label.cget("text") == "1 selected" and window.tree.set(conv.key, "sel") == "☑"
    assert not window.sync_selected_button.instate(["disabled"])
    window.toggle_all_shown()
    assert len(window.selected_items()) == len([c for c in window.shown_items() if not c.is_empty])
    window.toggle_all_shown()
    assert window.selected_items() == []


# --------------------------------------------------------------------------- single sync, both directions
def test_import_cursor_chat_into_claude_through_the_ui(window: MainWindow, world: World, answers: Answers) -> None:
    conv = conv_by(window, cursor_id=CHAT_MAIN)
    focus(window, conv)
    answers.queue = [False]
    window.sync_focused()
    assert pump(window, lambda: bool(answers.asked))
    assert logs(world) == [], "cancelling the confirmation must write nothing"
    answers.queue = [True]
    window.sync_focused()
    assert pump(window, lambda: conv_by(window, cursor_id=CHAT_MAIN).state is SyncState.IN_SYNC and bool(window.last_reports)), (
        "never became In sync"
    )
    assert session_uuid(CHAT_MAIN) in {p.stem for p in logs(world)}
    assert (world.paths.desktop_dir / "org" / "acct" / f"{local_session_name(CHAT_MAIN)}.json").exists()


def test_session_files_use_unix_newlines(window: MainWindow, world: World, answers: Answers) -> None:
    focus(window, conv_by(window, cursor_id=CHAT_MAIN))
    answers.queue = [True]
    window.sync_focused()
    assert pump(window, lambda: bool(window.last_reports))
    log = next(p for p in logs(world) if p.stem == session_uuid(CHAT_MAIN))
    meta = world.paths.desktop_dir / "org" / "acct" / f"{local_session_name(CHAT_MAIN)}.json"
    assert b"\r\n" not in log.read_bytes() and b"\r\n" not in meta.read_bytes(), "Windows must not turn JSON files into CRLF"


def test_send_claude_session_to_cursor_then_undo_from_activity(window: MainWindow, world: World, answers: Answers) -> None:
    before = cursor_chat_count(world)
    focus(window, conv_by(window, claude_key=CLAUDE_KEY))
    answers.queue = [True]
    window.sync_focused()
    assert pump(window, lambda: conv_by(window, claude_key=CLAUDE_KEY).state is SyncState.IN_SYNC and bool(window.last_reports))
    assert cursor_chat_count(world) == before + 1
    journals = window.sync_service.list_journals()
    assert len(journals) == 1 and journals[0].created and journals[0].messages == 6
    assert pump(window, lambda: len(window.activity.journals) == 1)
    assert "Created chat" in str(window.activity.journal_tree.item("0", "values")[0])

    window.activity.journal_tree.selection_set("0")
    answers.queue = [True]
    window.activity.undo_selected()
    assert pump(
        window, lambda: cursor_chat_count(world) == before and conv_by(window, claude_key=CLAUDE_KEY).state is SyncState.CLAUDE_ONLY
    )
    assert window.sync_service.list_journals() == []
    assert (world.paths.desktop_dir / "org" / "acct" / f"{CLAUDE_KEY}.json").exists(), "the Claude session itself must be untouched"


def test_follow_up_in_claude_is_compared_and_synced_to_cursor(window: MainWindow, world: World, answers: Answers) -> None:
    window.sync_service.sync(conv_by(window, claude_key=CLAUDE_KEY), apply=True)
    reload_and_wait(window)
    claude_follow_up(world, CLAUDE_CLI, [("u", "add docs"), ("a", "docs added")])
    reload_and_wait(window)
    conv = conv_by(window, claude_key=CLAUDE_KEY)
    assert conv.state is SyncState.CLAUDE_CHANGED
    focus(window, conv)
    window.compare_focused()
    assert pump(window, lambda: "2 message(s) will be added to Cursor" in window.detail.result.cget("text"))
    answers.queue = [True]
    window.sync_focused()
    assert pump(window, lambda: conv_by(window, claude_key=CLAUDE_KEY).state is SyncState.IN_SYNC and bool(window.last_reports))
    assert window.last_reports[0].status == "synced" and window.last_reports[0].to_cursor == 2


def test_unlink_forgets_the_pairing_only(window: MainWindow, world: World, answers: Answers) -> None:
    window.sync_service.sync(conv_by(window, cursor_id=CHAT_MAIN), apply=True)
    reload_and_wait(window)
    focus(window, conv_by(window, cursor_id=CHAT_MAIN))
    assert window.detail.unlink_button.winfo_ismapped()
    answers.queue = [True]
    window.unlink_focused()
    assert pump(window, lambda: window.sync_service.links.all() == [])
    assert len(logs(world)) == 1, "unlinking must not delete the Claude session"


# --------------------------------------------------------------------------- bulk
def test_bulk_compare_then_sync_selected(window: MainWindow, world: World, answers: Answers) -> None:
    window.select_all_shown()
    count = len(window.selected_items())
    assert count >= 4 and window.selected_label.cget("text") == f"{count} selected"
    chats_before = cursor_chat_count(world)
    window.compare_selected()
    assert pump(window, lambda: isinstance(window.report_dialog, ReportDialog))
    assert window.report_dialog is not None and "dry-run" in window.report_dialog.summary_label.cget("text")
    assert logs(world) == [] and cursor_chat_count(world) == chats_before, "a comparison must not write anything"
    window.report_dialog.destroy()
    window.report_dialog = None
    answers.queue = [True]
    window.sync_selected()
    assert pump(window, lambda: window.report_dialog is not None)
    assert window.report_dialog is not None and "synced" in window.report_dialog.summary_label.cget("text")
    assert {p.stem for p in logs(world)} >= {session_uuid(CHAT_MAIN), session_uuid(CHAT_BACKUP_ONLY), session_uuid(CHAT_TRANSCRIPT)}
    assert pump(window, lambda: any(c.state is SyncState.IN_SYNC for c in window.all_items())), "list never refreshed after the sync"


def test_bulk_cancel_confirmation_writes_nothing(window: MainWindow, world: World, answers: Answers) -> None:
    window.select_all_shown()
    answers.queue = [False]
    window.sync_selected()
    pump(window, lambda: False, timeout=0.5)  # give any (wrongly) started work a chance to run
    assert logs(world) == [] and window.sync_service.links.all() == [], "cancelling the confirmation must write nothing"


# --------------------------------------------------------------------------- safety UX
def test_cursor_open_shows_banner_and_defers_cursor_writes(
    window: MainWindow, world: World, answers: Answers, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.sync_service.sync(conv_by(window, claude_key=CLAUDE_KEY), apply=True)
    reload_and_wait(window)
    claude_follow_up(world, CLAUDE_CLI, [("u", "while cursor is open"), ("a", "noted")])
    reload_and_wait(window)
    monkeypatch.setattr("chatbridge.tkgui.window.cursor_running", lambda profile: True)
    monkeypatch.setattr(cursor_writer, "cursor_running", lambda profile: True)
    window._poll_running = False
    window._poll_cursor()
    assert pump(window, lambda: window.banner_visible and "Cursor is open" in window.banner.cget("text"))
    focus(window, conv_by(window, claude_key=CLAUDE_KEY))
    answers.queue = [True]
    window.sync_focused()
    assert pump(window, lambda: bool(window.last_reports))
    assert window.last_reports[0].status == "deferred" and "Close Cursor" in window.last_reports[0].detail
    assert pump(window, lambda: conv_by(window, claude_key=CLAUDE_KEY).state is SyncState.CLAUDE_CHANGED)
    monkeypatch.undo()
    window._poll_running = False
    window._poll_cursor()
    assert pump(window, lambda: not window.banner_visible)


def test_failed_conversation_is_reported_not_fatal(window: MainWindow, world: World, answers: Answers) -> None:
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    conn.execute("UPDATE cursorDiskKV SET value = 'broken' WHERE key LIKE ?", (f"bubbleId:{CHAT_MAIN}:%",))
    conn.commit()
    conn.close()
    window.ticked |= {conv_by(window, cursor_id=CHAT_MAIN).key, conv_by(window, cursor_id=CHAT_BACKUP_ONLY).key}
    answers.queue = [True]
    window.sync_selected()
    assert pump(window, lambda: window.report_dialog is not None)
    assert window.report_dialog is not None
    summary = window.report_dialog.summary_label.cget("text")
    assert "1 synced" in summary and "1 failed" in summary
    assert [p.stem for p in logs(world)] == [session_uuid(CHAT_BACKUP_ONLY)]
    assert not window.busy


# --------------------------------------------------------------------------- auto-sync, settings, profiles
def test_auto_sync_keeps_a_linked_conversation_up_to_date(window: MainWindow, world: World) -> None:
    window.sync_service.sync(conv_by(window, claude_key=CLAUDE_KEY), apply=True)
    reload_and_wait(window)
    window.autosync.interval = 0.2
    window.auto_var.set(True)
    window._on_auto_toggled()
    assert window.autosync.running and load_settings(world.paths).auto_sync is True
    claude_follow_up(world, CLAUDE_CLI, [("u", "typed while auto-sync runs"), ("a", "answer")])
    assert pump(window, lambda: any("Synced 1 conversation" in m for m in window.activity.log_messages()), timeout=30), (
        "auto-sync never reported"
    )
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    texts = [json.loads(v).get("text") for (v,) in conn.execute("SELECT value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'")]
    conn.close()
    assert "typed while auto-sync runs" in texts
    window.auto_var.set(False)
    window._on_auto_toggled()
    assert pump(window, lambda: not window.autosync.running) and load_settings(world.paths).auto_sync is False


def test_auto_sync_never_imports_unlinked_conversations(window: MainWindow, world: World) -> None:
    window.autosync.interval = 0.2
    window.auto_var.set(True)
    window._on_auto_toggled()
    pump(window, lambda: False, timeout=1.5)
    window.auto_var.set(False)
    window._on_auto_toggled()
    assert logs(world) == [] and window.sync_service.links.all() == []


def test_settings_persist(window: MainWindow, world: World) -> None:
    window.empty_var.set(True)
    window.subagent_var.set(True)
    window._update_criteria()
    saved = load_settings(world.paths)
    assert saved.show_empty and saved.show_subagents
    assert saved.extra_profiles == [("backup", str(world.backup_user))]


def test_profiles_dialog_validates_and_persists(window: MainWindow, world: World, tmp_path: Path) -> None:
    dialog = window.show_profiles()
    assert isinstance(dialog, ProfilesDialog)
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    assert dialog.add_path(plain) is False and "state.vscdb" in dialog.error_label.cget("text")
    assert dialog.add_path(world.backup_user) is False and "already" in dialog.error_label.cget("text")
    assert window.settings.extra_profiles == [("backup", str(world.backup_user))]
    dialog.destroy()


def test_activity_page_is_reachable(window: MainWindow) -> None:
    window.notebook.select(window.activity)  # type: ignore[no-untyped-call]
    window.update()
    assert str(window.notebook.select()) == str(window.activity)  # type: ignore[no-untyped-call]
    assert window.activity.journals == [] and window.activity.journal_tree.exists("none"), "the empty-state row must be present"


def test_carry_context_menu_toggle_is_persisted(window: MainWindow, world: World) -> None:
    assert window.settings.carry_context is False
    window.carry_var.set(True)
    window._on_carry_toggled()
    assert window.settings.carry_context is True and load_settings(world.paths).carry_context is True
    window.carry_var.set(False)
    window._on_carry_toggled()
    assert load_settings(world.paths).carry_context is False


def test_real_confirmation_dialog_returns_the_choice(window: MainWindow) -> None:
    """The modal dialog itself (not the test seam): Cancel and Escape say no, the confirm button says yes."""

    def press(label: str) -> None:
        for dialog in window.winfo_children():
            if isinstance(dialog, tk.Toplevel):
                for widget in _walk(dialog):
                    if widget.winfo_class() == "TButton" and str(widget.cget("text")).endswith(label):
                        widget.invoke()  # type: ignore[attr-defined]
                        return

    def _walk(widget: tk.Misc) -> Iterator[tk.Misc]:
        yield widget
        for child in widget.winfo_children():
            yield from _walk(child)

    window.after(200, lambda: press("Sync"))
    assert ask_confirm(window, "Sync?", "body", "Sync") is True
    window.after(200, lambda: press("Cancel"))
    assert ask_confirm(window, "Sync?", "body", "Sync") is False
