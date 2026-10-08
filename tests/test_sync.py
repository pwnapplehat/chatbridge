"""Two-way sync: pairing, state detection, merges, deferral, undo and round-trip fidelity."""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from chatbridge import cursor_writer
from chatbridge.claude_source import iter_claude_events, list_claude_sessions
from chatbridge.config import Settings
from chatbridge.cursor_source import iter_events
from chatbridge.cursor_writer import CursorBusyError, CursorWriteError, undo_journal, upsert_events
from chatbridge.events import event_key, meaningful, missing_events
from chatbridge.model import AssistantText, Event, Reasoning, ToolCall, UserText
from chatbridge.sync import Conversation, SyncService, SyncState, read_claude_events, read_cursor_events
from chatbridge.writer import local_session_name
from tests.fixtures import (
    CHAT_MAIN,
    CLAUDE_CLI,
    CLAUDE_KEY,
    T0,
    ClaudeLog,
    World,
    age,
    claude_follow_up,
    cursor_follow_up,
    write_claude_session,
)


@pytest.fixture
def sync(world: World) -> SyncService:
    return SyncService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))]))


def find(sync: SyncService, *, cursor_id: str | None = None, claude_key: str | None = None) -> Conversation:
    conversations, _ = sync.load_conversations()
    for conv in conversations:
        if cursor_id and conv.cursor and conv.cursor.ref.chat_id == cursor_id:
            return conv
        if claude_key and conv.claude and conv.claude.key == claude_key:
            return conv
    raise AssertionError("conversation not found")


def native_session(world: World, cwd: str | None = None) -> Path:
    """A Claude-native chat with thinking, a tool call, an error, harness noise and a system-reminder."""
    folder = cwd or str(world.project_dir)
    log = ClaudeLog(CLAUDE_CLI, folder)
    log.noise()
    log.user("Refactor the lexer please", reminder=True)
    log.assistant("Looking at it.", thinking="First read the file.", tool=("Read", {"file_path": "/src/lexer.py"}, "def lex(): ...", False))
    log.assistant("", tool=("Bash", {"command": "pytest"}, "1 failed", True))
    log.assistant("Fixed the failing test.")
    return write_claude_session(world, CLAUDE_KEY, CLAUDE_CLI, "Refactor lexer", log, folder)


def keys(events: list[Event]) -> list[str | None]:
    return [event_key(e) for e in meaningful(events)]


# --------------------------------------------------------------------------- reading Claude
def test_claude_reader_skips_noise_and_maps_blocks(world: World, sync: SyncService) -> None:
    native_session(world)
    (session,) = [s for s in list_claude_sessions(world.paths) if s.key == CLAUDE_KEY]
    events = meaningful(iter_claude_events(session))
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["UserText", "Reasoning", "AssistantText", "ToolCall", "ToolCall", "AssistantText"]
    assert isinstance(events[0], UserText) and events[0].text == "Refactor the lexer please", "system-reminder must be stripped"
    tools = [e for e in events if isinstance(e, ToolCall)]
    assert (tools[0].name, tools[0].output, tools[0].is_error) == ("Read", "def lex(): ...", False)
    assert (tools[1].name, tools[1].is_error) == ("Bash", True)


def test_claude_session_listing_titles_and_counts(world: World) -> None:
    native_session(world)
    (session,) = list_claude_sessions(world.paths)
    assert session.title == "Refactor lexer" and session.key == CLAUDE_KEY and session.record_count > 0


# --------------------------------------------------------------------------- Cursor -> Claude
def test_cursor_chat_syncs_to_new_claude_session_and_links(sync: SyncService, world: World) -> None:
    conv = find(sync, cursor_id=CHAT_MAIN)
    assert conv.state is SyncState.CURSOR_ONLY
    report = sync.sync(conv, apply=True)
    assert report.status == "synced" and report.to_claude > 0
    paired = find(sync, cursor_id=CHAT_MAIN)
    assert paired.claude is not None and paired.claude.key == local_session_name(CHAT_MAIN)
    assert paired.state is SyncState.IN_SYNC
    assert sync.sync(paired, apply=True).status == "noop"


def test_dry_run_changes_nothing(sync: SyncService, world: World) -> None:
    report = sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=False)
    assert report.status == "dry-run" and report.to_claude > 0
    assert list((world.paths.claude_dir / "projects").glob("*/*.jsonl")) == []
    assert sync.links.all() == []


# --------------------------------------------------------------------------- Claude -> Cursor
def test_claude_session_syncs_to_new_cursor_chat_with_exact_fidelity(sync: SyncService, world: World) -> None:
    native_session(world)
    conv = find(sync, claude_key=CLAUDE_KEY)
    assert conv.state is SyncState.CLAUDE_ONLY
    report = sync.sync(conv, apply=True)
    assert report.status == "synced" and report.to_cursor == 6 and report.journal
    paired = find(sync, claude_key=CLAUDE_KEY)
    assert paired.cursor is not None and paired.state is SyncState.IN_SYNC
    assert keys(read_cursor_events(paired.cursor.ref)) == keys(read_claude_events(paired.claude))  # type: ignore[arg-type]
    cursor_events = read_cursor_events(paired.cursor.ref)
    tools = [e for e in cursor_events if isinstance(e, ToolCall)]
    assert tools[0].output == "def lex(): ..." and tools[1].is_error
    assert sync.sync(paired, apply=True).status == "noop"


def test_new_cursor_chat_is_placed_in_the_matching_workspace(sync: SyncService, world: World) -> None:
    native_session(world)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    (workspace,) = conn.execute(
        "SELECT workspaceId FROM composerHeaders WHERE composerId LIKE '%' AND isSubagent = 0 AND createdAt = ?", (T0 + 1000,)
    ).fetchall() or [("?",)]
    row = conn.execute(
        "SELECT workspaceId, isArchived, value FROM composerHeaders WHERE workspaceId = 'ws1' AND composerId NOT IN (?, ?)",
        (CHAT_MAIN, "22222222-aaaa-4aaa-8aaa-000000000002"),
    ).fetchone()
    conn.close()
    assert row is not None, "the new chat must be filed under the workspace of its folder"
    head = json.loads(row[2])
    assert head["type"] == "head" and head["name"] == "Refactor lexer" and head["workspaceIdentifier"]["id"] == "ws1"
    assert workspace  # header row exists


def test_unknown_folder_goes_to_no_folder_workspace(sync: SyncService, world: World, tmp_path: Path) -> None:
    other = tmp_path / "somewhere-else"
    other.mkdir()
    native_session(world, str(other))
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    ids = [r[0] for r in conn.execute("SELECT workspaceId FROM composerHeaders WHERE value LIKE '%Refactor lexer%'")]
    conn.close()
    assert ids == ["empty-window"]


# --------------------------------------------------------------------------- follow-ups
def test_follow_up_in_claude_flows_to_cursor_then_cursor_follow_up_flows_back(sync: SyncService, world: World) -> None:
    native_session(world)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)

    claude_follow_up(world, CLAUDE_CLI, [("u", "Now add tests"), ("a", "Added tests.")])
    changed = find(sync, claude_key=CLAUDE_KEY)
    assert changed.state is SyncState.CLAUDE_CHANGED
    report = sync.sync(changed, apply=True)
    assert (report.status, report.to_cursor, report.to_claude) == ("synced", 2, 0)
    assert find(sync, claude_key=CLAUDE_KEY).state is SyncState.IN_SYNC

    conv = find(sync, claude_key=CLAUDE_KEY)
    assert conv.cursor is not None
    cursor_follow_up(
        conv.cursor.ref.source_path, conv.cursor.ref.chat_id, [(1, "And update the docs"), (2, "Docs updated.")], T0 + 9_000_000
    )
    changed = find(sync, claude_key=CLAUDE_KEY)
    assert changed.state is SyncState.CURSOR_CHANGED
    report = sync.sync(changed, apply=True)
    assert (report.status, report.to_claude, report.to_cursor) == ("synced", 2, 0)

    final = find(sync, claude_key=CLAUDE_KEY)
    assert final.state is SyncState.IN_SYNC
    claude_events = read_claude_events(final.claude)  # type: ignore[arg-type]
    assert [e.text for e in claude_events if isinstance(e, UserText)] == [
        "Refactor the lexer please",
        "Now add tests",
        "And update the docs",
    ]
    assert isinstance(claude_events[-1], AssistantText) and claude_events[-1].text == "Docs updated."
    assert sorted(k or "" for k in keys(read_cursor_events(final.cursor.ref))) == sorted(k or "" for k in keys(claude_events))  # type: ignore[union-attr]


def test_cursor_origin_chat_continued_in_claude_syncs_back(sync: SyncService, world: World) -> None:
    sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=True)
    paired = find(sync, cursor_id=CHAT_MAIN)
    assert paired.claude is not None
    claude_follow_up(
        world,
        paired.claude.cli_id,
        [("u", "one more thing"), ("a", "done", ("Write", {"file_path": "/a"}, "ok", False))],
    )
    changed = find(sync, cursor_id=CHAT_MAIN)
    assert changed.state is SyncState.CLAUDE_CHANGED
    report = sync.sync(changed, apply=True)
    assert report.status == "synced" and report.to_cursor == 3 and report.to_claude == 0
    after = find(sync, cursor_id=CHAT_MAIN)
    texts = [e.text for e in read_cursor_events(after.cursor.ref) if isinstance(e, UserText)]  # type: ignore[union-attr]
    assert texts[-1] == "one more thing"
    assert sync.sync(after, apply=True).status == "noop"


def test_both_sides_continued_merge_without_loss_or_duplicates(sync: SyncService, world: World) -> None:
    native_session(world)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    conv = find(sync, claude_key=CLAUDE_KEY)
    claude_follow_up(world, CLAUDE_CLI, [("u", "claude question"), ("a", "claude answer")])
    cursor_follow_up(conv.cursor.ref.source_path, conv.cursor.ref.chat_id, [(1, "cursor question"), (2, "cursor answer")], T0 + 20_000_000)  # type: ignore[union-attr]
    diverged = find(sync, claude_key=CLAUDE_KEY)
    assert diverged.state is SyncState.BOTH_CHANGED
    report = sync.sync(diverged, apply=True)
    assert (report.status, report.to_claude, report.to_cursor) == ("synced", 2, 2)
    merged = find(sync, claude_key=CLAUDE_KEY)
    claude_texts = [e.text for e in read_claude_events(merged.claude) if isinstance(e, (UserText, AssistantText))]  # type: ignore[arg-type]
    cursor_texts = [e.text for e in read_cursor_events(merged.cursor.ref) if isinstance(e, (UserText, AssistantText))]  # type: ignore[union-attr]
    for texts in (claude_texts, cursor_texts):
        for needle in ("claude question", "claude answer", "cursor question", "cursor answer"):
            assert texts.count(needle) == 1, f"{needle!r} must appear exactly once"
    assert sorted(claude_texts) == sorted(cursor_texts)
    assert merged.state is SyncState.IN_SYNC and sync.sync(merged, apply=True).status == "noop"


def test_repeated_identical_messages_are_not_lost_or_duplicated(sync: SyncService, world: World) -> None:
    native_session(world)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    claude_follow_up(world, CLAUDE_CLI, [("u", "continue"), ("a", "ok")] * 3)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    conv = find(sync, claude_key=CLAUDE_KEY)
    cursor_users = [e.text for e in read_cursor_events(conv.cursor.ref) if isinstance(e, UserText)]  # type: ignore[union-attr]
    assert cursor_users.count("continue") == 3
    assert sync.sync(conv, apply=True).status == "noop"


def test_fork_session_is_followed_through_the_desktop_record(sync: SyncService, world: World) -> None:
    """The Claude app re-keys a continued session (new cliSessionId, history copied); sync must use the new log."""
    path = native_session(world)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    forked = path.with_name("cccccccc-1111-4111-8111-000000000001.jsonl")
    forked.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    meta_path = world.paths.desktop_dir / "org" / "acct" / f"{CLAUDE_KEY}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["cliSessionId"] = forked.stem
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    claude_follow_up(world, forked.stem, [("u", "after fork"), ("a", "fork reply")])
    report = sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    assert report.status == "synced" and report.to_cursor == 2


# --------------------------------------------------------------------------- safety
def test_cursor_running_defers_cursor_writes_but_not_claude_ones(sync: SyncService, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    native_session(world)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    claude_follow_up(world, CLAUDE_CLI, [("u", "while cursor is open"), ("a", "noted")])
    conv = find(sync, claude_key=CLAUDE_KEY)
    cursor_follow_up(conv.cursor.ref.source_path, conv.cursor.ref.chat_id, [(1, "typed in cursor")], T0 + 30_000_000)  # type: ignore[union-attr]
    monkeypatch.setattr(cursor_writer, "cursor_running", lambda profile: True)
    report = sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    assert report.status == "deferred" and "Close Cursor" in report.detail
    assert report.to_claude == 1 and report.to_cursor == 2
    state = find(sync, claude_key=CLAUDE_KEY)
    assert state.state is SyncState.CLAUDE_CHANGED, "the unsynced Claude messages must still be flagged"
    monkeypatch.undo()
    assert sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True).status == "synced"
    assert find(sync, claude_key=CLAUDE_KEY).state is SyncState.IN_SYNC


def test_claude_log_changed_moments_ago_is_deferred(sync: SyncService, world: World) -> None:
    sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=True)
    paired = find(sync, cursor_id=CHAT_MAIN)
    cursor_follow_up(paired.cursor.ref.source_path, CHAT_MAIN, [(1, "new in cursor")], T0 + 40_000_000)  # type: ignore[union-attr]
    log = paired.claude.log_path  # type: ignore[union-attr]
    log.touch()  # modified "now": the app may be mid-turn
    report = sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=True)
    assert report.status == "deferred" and "mid-turn" in report.detail
    age(log)
    assert sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=True).status == "synced"


def test_read_only_profile_is_refused(world: World) -> None:
    from chatbridge.cursor_source import CursorProfile

    with pytest.raises(CursorWriteError):
        upsert_events(CursorProfile(world.backup_user, "backup"), "x", "t", None, [UserText("hi", 1)], world.root / "j")


def test_undo_journal_restores_the_database_exactly(world: World) -> None:
    from chatbridge.cursor_source import CursorProfile

    profile = CursorProfile(world.live_user, "live", writable=True)

    def snapshot() -> tuple[list[tuple[str, str]], list[tuple[object, ...]]]:
        conn = sqlite3.connect(profile.db_path)
        data = (
            sorted((k, v) for k, v in conn.execute("SELECT key, value FROM cursorDiskKV")),
            sorted(conn.execute("SELECT * FROM composerHeaders").fetchall()),
        )
        conn.close()
        return data

    before = snapshot()
    new = upsert_events(
        profile,
        "99999999-aaaa-4aaa-8aaa-000000000009",
        "New chat",
        None,
        [UserText("hi", T0), AssistantText("hello", T0 + 1)],
        world.root / "journal",
    )
    appended = upsert_events(profile, CHAT_MAIN, "Fix the parser", None, [UserText("extra", T0 + 5)], world.root / "journal")
    assert snapshot() != before
    undo_journal(profile, appended.journal_path)  # type: ignore[arg-type]
    undo_journal(profile, new.journal_path)  # type: ignore[arg-type]
    assert snapshot() == before


def test_busy_cursor_refuses_direct_write(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from chatbridge.cursor_source import CursorProfile

    monkeypatch.setattr(cursor_writer, "cursor_running", lambda profile: True)
    with pytest.raises(CursorBusyError):
        upsert_events(CursorProfile(world.live_user, "live", writable=True), "x", "t", None, [UserText("hi", 1)], world.root / "j")


def test_unlink_forgets_pair_but_keeps_conversations(sync: SyncService, world: World) -> None:
    sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=True)
    sync.unlink(find(sync, cursor_id=CHAT_MAIN))
    assert sync.links.all() == []
    assert find(sync, cursor_id=CHAT_MAIN).claude is not None, "pair is re-detected from deterministic ids"


# --------------------------------------------------------------------------- round-trip properties
def random_events(rng: random.Random, count: int) -> list[Event]:
    words = ["alpha", "beta", "gamma", "δelta", "line1\nline2", "  padded  ", "emoji 🙂", '{"json": 1}', "a" * 300]
    events: list[Event] = []
    ts = T0
    for index in range(count):
        ts += rng.randint(1, 5000)
        kind = rng.choice(["u", "a", "r", "t"])
        text = f"{rng.choice(words)} #{index}"
        if kind == "u":
            events.append(UserText(text, ts))
        elif kind == "a":
            events.append(AssistantText(text, ts))
        elif kind == "r":
            events.append(Reasoning(text, ts))
        else:
            events.append(
                ToolCall(
                    index,
                    f"id{index}",
                    rng.choice(["Read", "Bash", "mcp__x__y"]),
                    {"arg": text, "n": index},
                    text if rng.random() > 0.2 else None,
                    rng.random() < 0.2,
                    ts,
                )
            )
    return events


def test_events_survive_a_round_trip_through_claude_and_cursor(world: World) -> None:
    from chatbridge.cursor_source import CursorProfile
    from chatbridge.model import ChatRef

    rng = random.Random(1234)
    profile = CursorProfile(world.live_user, "live", writable=True)
    for trial in range(25):
        events = meaningful(random_events(rng, rng.randint(1, 40)))
        chat_id = f"{trial:08d}-aaaa-4aaa-8aaa-00000000{trial:04d}"
        upsert_events(profile, chat_id, f"trial {trial}", None, events, world.root / "journal")
        ref = ChatRef(chat_id, "", 0, None, False, "db", profile.db_path, "live", len(events))
        back = meaningful(iter_events(ref))
        assert keys(back) == keys(events), f"Cursor round trip lost or changed events in trial {trial}"
        assert not missing_events(events, back) and not missing_events(back, events)
        outputs = {(e.name, json.dumps(e.tool_input, sort_keys=True)): e.output for e in events if isinstance(e, ToolCall)}
        for e in back:
            if isinstance(e, ToolCall):
                assert e.output == outputs[(e.name, json.dumps(e.tool_input, sort_keys=True))] or e.output is None


def test_cursor_chat_syncs_to_claude_code_cli_when_the_desktop_app_has_no_sessions(sync: SyncService, world: World) -> None:
    """Claude Code CLI users have logs but no desktop sidebar records; the session must still be written and linked."""
    for meta in world.paths.desktop_dir.glob("*/*/local_*.json"):
        meta.unlink()
    report = sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=True)
    assert report.status == "synced" and "claude --resume" in report.detail
    paired = find(sync, cursor_id=CHAT_MAIN)
    assert paired.claude is not None and paired.claude.meta_path is None and paired.state is SyncState.IN_SYNC
    assert sync.sync(paired, apply=True).status == "noop"


def test_read_only_commands_leave_no_files_behind(sync: SyncService, world: World) -> None:
    sync.load_conversations()
    sync.sync(find(sync, cursor_id=CHAT_MAIN), apply=False)
    assert not world.paths.links_db.exists() and not world.paths.data_dir.exists(), "listing and dry runs must not create the link database"
