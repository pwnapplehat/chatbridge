"""Compaction of big imports so Claude's context stays inside its window, without losing or duplicating history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatbridge.compaction import (
    FULL_BUDGET_TOKENS,
    MAX_OUTPUT_CHARS,
    EventScan,
    capped_output,
    plan_compaction,
    wrap_summary,
)
from chatbridge.model import AssistantText, ChatRef, Event, ToolCall, UserText
from chatbridge.validate import validate
from chatbridge.writer import count_written, import_chat
from tests.fixtures import World


def big_events(turns: int = 120, output_chars: int = 6000) -> list[Event]:
    events: list[Event] = []
    for turn in range(turns):
        base = 1_788_000_000_000 + turn * 60_000
        events += [
            UserText(f"request number {turn}: please look at module {turn}", base),
            ToolCall(0, f"c{turn}", "Read", {"file_path": f"/src/m{turn}.py"}, "x" * output_chars, False, base + 1000),
            AssistantText(f"answer number {turn}", base + 2000),
        ]
    return events


def scan_of(events: list[Event]) -> EventScan:
    scan = EventScan()
    for event in events:
        scan.add(event)
    return scan


def entries(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_small_conversations_are_not_compacted() -> None:
    assert plan_compaction(scan_of(big_events(3)), "t", "src") is None


def test_plan_cuts_at_a_user_turn_and_summarizes_every_older_request() -> None:
    events = big_events()
    plan = plan_compaction(scan_of(events), "My chat", "Cursor (live)")
    assert plan is not None and isinstance(events[plan.recent_start], UserText)
    assert plan.post_tokens < FULL_BUDGET_TOKENS // 2 < plan.pre_tokens
    assert "request number 0" in plan.summary and f"request number {plan.recent_start // 3 - 1}" in plan.summary
    assert f"request number {plan.recent_start // 3}" not in plan.summary, "the cut turn itself is in the verbatim part"
    wrapped = wrap_summary(plan, "/x/log.jsonl")
    assert wrapped.startswith("This session is being continued from a previous conversation that ran out of context.")
    assert "/x/log.jsonl" in wrapped and "Recent messages are preserved verbatim." in wrapped


def test_summary_is_capped_and_says_when_requests_were_left_out() -> None:
    events: list[Event] = []
    for turn in range(2000):
        events += [UserText(f"request {turn} " + "word " * 100, turn), AssistantText("ok " * 10, turn)]
    events += big_events(30)
    plan = plan_compaction(scan_of(events), "Huge", "src")
    assert plan is not None and len(plan.summary) <= 48_000 and plan.truncated_requests
    assert "earlier requests omitted" in plan.summary and "request 0 " in plan.summary
    assert "earliest part of the conversation was too large" in wrap_summary(plan, "/p")


def test_tool_outputs_in_the_context_copies_are_capped() -> None:
    assert capped_output("y" * 50) == "y" * 50 and capped_output(None) is None
    assert len(capped_output("z" * (MAX_OUTPUT_CHARS + 500)) or "") < MAX_OUTPUT_CHARS + 200


def test_import_writes_full_history_then_boundary_summary_and_flagged_recent_copies(tmp_path: Path) -> None:
    events = big_events()
    chat = ChatRef("big-chat", "Big chat", 1, None, False, "db", Path("/x"), "live", len(events))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    report = import_chat(chat, lambda: iter(events), tmp_path / "claude", sessions, True)
    assert report.status == "written" and report.verified and "compacted" in report.detail
    log = Path(report.log_path)
    assert validate(log) == [], "chain, tool pairing and timestamps stay valid across the boundary"
    lines = entries(log)
    boundary = next(i for i, e in enumerate(lines) if e.get("subtype") == "compact_boundary")
    b = lines[boundary]
    assert b["parentUuid"] is None and b["compactMetadata"]["trigger"] == "manual" and b["logicalParentUuid"]  # type: ignore[index]
    summary = lines[boundary + 1]
    assert summary["isCompactSummary"] is True and summary["parentUuid"] == b["uuid"]
    before = [e for e in lines[:boundary] if e.get("type") in ("user", "assistant")]
    after = [e for e in lines[boundary + 2 :] if e.get("type") in ("user", "assistant")]
    assert all(not e.get("chatbridgeRecap") for e in before), "the full history before the boundary is untouched"
    assert after and all(e.get("chatbridgeRecap") for e in after)
    assert len(before) > 0
    assert count_written(log).content_tuple() == report.source_counts.content_tuple(), "copies are not counted as conversation content"
    # the active chain (what the model gets) is small
    active_chars = sum(len(json.dumps(e)) for e in lines[boundary:] if e.get("type") in ("user", "assistant"))
    assert active_chars / 4 < FULL_BUDGET_TOKENS


def test_dry_run_reports_the_compaction_without_writing(tmp_path: Path) -> None:
    events = big_events()
    chat = ChatRef("big-chat-2", "Big", 1, None, False, "db", Path("/x"), "live", len(events))
    report = import_chat(chat, lambda: iter(events), tmp_path / "claude", tmp_path, False)
    assert report.status == "dry-run" and "compacted to about" in report.detail
    assert not (tmp_path / "claude").exists()


# --------------------------------------------------------------------------- repair of an oversized imported session
def add_huge_cursor_chat(world: object, chat_id: str, turns: int = 160) -> None:
    import sqlite3

    from tests.fixtures import CREATED_MS, World, _iso, add_chat

    assert isinstance(world, World)
    bubbles: list[dict[str, object]] = []
    for turn in range(turns):
        base = CREATED_MS + turn * 120_000
        bubbles.append(
            {"_v": 3, "type": 1, "bubbleId": f"u{turn:04d}", "createdAt": _iso(base), "text": f"question {turn}: " + "details " * 190}
        )
        bubbles.append(
            {
                "_v": 3,
                "type": 2,
                "bubbleId": f"a{turn:04d}",
                "createdAt": _iso(base + 5000),
                "text": f"answer {turn}: " + "explanation " * 160,
            }
        )
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    add_chat(conn, chat_id, "Huge Cursor chat", bubbles, list(range(len(bubbles))), "ws1", world.project_dir)
    conn.commit()
    conn.close()


def test_oversized_imported_session_is_compacted_by_a_sync_and_stays_in_sync(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from chatbridge import writer
    from chatbridge.claude_source import active_context_tokens
    from chatbridge.config import Settings
    from chatbridge.events import event_key
    from chatbridge.sync import SyncService, SyncState, read_claude_events, read_cursor_events
    from tests.fixtures import age, claude_follow_up

    chat_id = "77777777-aaaa-4aaa-8aaa-000000000007"
    add_huge_cursor_chat(world, chat_id)
    sync = SyncService(world.paths, Settings())
    # Reproduce the reported situation: a version that imported the whole history without compaction.
    with monkeypatch.context() as patch:
        patch.setattr(writer, "plan_compaction", lambda *args, **kwargs: None)
        conv = next(c for c in sync.load_conversations()[0] if c.cursor and c.cursor.ref.chat_id == chat_id)
        assert sync.sync(conv, apply=True).status == "synced"
    conv = next(c for c in sync.load_conversations()[0] if c.cursor and c.cursor.ref.chat_id == chat_id)
    assert conv.claude is not None
    assert active_context_tokens(conv.claude.log_path) > FULL_BUDGET_TOKENS, "the imported session is far over the window"
    age(conv.claude.log_path)
    before = [event_key(e) for e in read_claude_events(conv.claude)]

    dry = sync.sync(conv, apply=False)
    assert any("compact the Claude session" in f for f in dry.fixes) and (dry.to_claude, dry.to_cursor) == (0, 0)
    assert active_context_tokens(conv.claude.log_path) > FULL_BUDGET_TOKENS, "a dry run must not write"
    report = sync.sync(conv, apply=True)
    assert report.status == "synced"

    after_conv = next(c for c in sync.load_conversations()[0] if c.cursor and c.cursor.ref.chat_id == chat_id)
    assert after_conv.claude is not None and validate(after_conv.claude.log_path) == []
    assert active_context_tokens(after_conv.claude.log_path) < FULL_BUDGET_TOKENS // 2, "the active context is now small"
    assert [event_key(e) for e in read_claude_events(after_conv.claude)] == before, "no message was added, removed or duplicated"
    assert sync.plan(after_conv) == ([], []), "Cursor and Claude still hold exactly the same messages"
    assert sync.sync(after_conv, apply=True).status == "noop", "idempotent: the compaction is not repeated"
    assert len(read_cursor_events(after_conv.cursor.ref)) == len(before) // 1  # type: ignore[union-attr]

    age(after_conv.claude.log_path)
    claude_follow_up(world, after_conv.claude.cli_id, [("u", "a follow-up after the compaction"), ("a", "reply")])
    follow = next(c for c in sync.load_conversations()[0] if c.cursor and c.cursor.ref.chat_id == chat_id)
    assert follow.state is SyncState.CLAUDE_CHANGED
    synced = sync.sync(follow, apply=True)
    assert (synced.status, synced.to_cursor, synced.to_claude) == ("synced", 2, 0)
