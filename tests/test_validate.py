"""Tests for the structural log validator and the opening-message guarantee."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from chatbridge.converter import OPENER_PREFIX, convert
from chatbridge.model import AssistantText, ChatRef, Event, Reasoning, ToolCall
from chatbridge.validate import validate
from chatbridge.writer import count_written, import_chat


def assistant_first_events() -> list[Event]:
    """Like a subagent run: Cursor never stored the first user message."""
    return [
        Reasoning("plan", 1000),
        ToolCall(0, "c1", "Read", {"path": "/x"}, "body", False, 1100),
        AssistantText("result", 1200),
    ]


def write_log(directory: Path, lines: list[dict[str, object]]) -> Path:
    path = directory / "log.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_assistant_first_chat_gets_honest_opening_user_message() -> None:
    entries = list(convert(assistant_first_events(), "s", "c", "/tmp", "t"))
    first = entries[0]
    assert first["type"] == "user"
    assert str(first["message"]["content"][0]["text"]).startswith(OPENER_PREFIX)  # type: ignore[index]
    with tempfile.TemporaryDirectory() as tmp:
        path = write_log(Path(tmp), entries)
        assert validate(path) == []
        counts = count_written(path)
        assert counts.user == 0, "the placeholder opener must not count as a human message"
        assert (counts.reasoning, counts.assistant_text, counts.tool_calls) == (1, 1, 1)


def test_import_chat_accepts_assistant_first_chat() -> None:
    chat = ChatRef("sub-1", "Subagent run", 1000, None, True, "db", Path("/x"), "t", 3)
    with tempfile.TemporaryDirectory() as tmp:
        sessions = Path(tmp) / "sessions"
        sessions.mkdir()
        report = import_chat(chat, assistant_first_events, Path(tmp) / "claude", sessions, True)
        assert report.status == "written" and report.verified


def test_validator_flags_broken_chain_unanswered_tool_and_empty_text() -> None:
    base: dict[str, object] = {"timestamp": "2026-01-01T00:00:00.000Z"}
    user: dict[str, object] = {
        **base,
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }
    tool: dict[str, object] = {
        **base,
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
    }
    chain_break: dict[str, object] = {
        **base,
        "type": "assistant",
        "uuid": "a2",
        "parentUuid": "WRONG",
        "message": {"role": "assistant", "content": [{"type": "text", "text": " "}]},
    }
    with tempfile.TemporaryDirectory() as tmp:
        problems = validate(write_log(Path(tmp), [user, tool, chain_break]))
    assert any("not answered" in p for p in problems)
    assert any("broken parent chain" in p for p in problems)
    assert any("empty text block" in p for p in problems)


def test_validator_flags_assistant_first_log_and_backwards_time() -> None:
    entry: dict[str, object] = {
        "type": "assistant",
        "uuid": "a",
        "parentUuid": None,
        "timestamp": "2026-01-02T00:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
    }
    later: dict[str, object] = {
        "type": "user",
        "uuid": "b",
        "parentUuid": "a",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "y"}]},
    }
    with tempfile.TemporaryDirectory() as tmp:
        problems = validate(write_log(Path(tmp), [entry, later]))
    assert any("first message is not from the user" in p for p in problems)
    assert any("timestamp goes backwards" in p for p in problems)
