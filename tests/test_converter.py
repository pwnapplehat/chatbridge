"""Unit tests for event -> Claude log conversion and the written-log verification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chatbridge.converter import REASONING_PREFIX, convert, count_event, safe_tool_name, tool_use_id
from chatbridge.cursor_source import key_range, parse_iso_ms
from chatbridge.model import AssistantText, ChatRef, Counts, Event, Reasoning, ToolCall, UserText
from chatbridge.writer import count_written, import_chat, resolve_cwd, session_uuid


def sample_events() -> list[Event]:
    return [
        UserText("hello", 1000),
        Reasoning("thinking about it", 1100),
        ToolCall(0, "call-a\nfc_1", "read_file_v2", {"path": "/x"}, "file body", False, 1200),
        ToolCall(1, "call-b", "run cmd!", {"command": "ls"}, None, True, 1300),
        AssistantText("done", 1400),
        UserText("thanks", 1500, image_count=2),
        AssistantText("welcome", 1600),
    ]


def tally(events: list[Event]) -> Counts:
    counts = Counts()
    for event in events:
        counts = count_event(counts, event)
    return counts


class ConverterTests(unittest.TestCase):
    def entries(self) -> list[dict[str, object]]:
        return list(convert(sample_events(), "sess", "chat", "/tmp", "[Cursor] t"))

    def test_every_tool_use_has_matching_result_in_next_user_entry(self) -> None:
        lines = [e for e in self.entries() if e["type"] in ("user", "assistant")]
        for index, entry in enumerate(lines):
            content = entry["message"]["content"]  # type: ignore[index]
            uses = [b["id"] for b in content if b["type"] == "tool_use"]
            if uses:
                following = lines[index + 1]["message"]["content"]  # type: ignore[index]
                self.assertEqual(uses, [b["tool_use_id"] for b in following if b["type"] == "tool_result"])

    def test_parent_chain_is_unbroken(self) -> None:
        parent = None
        for entry in self.entries():
            if "uuid" in entry:
                self.assertEqual(entry["parentUuid"], parent)
                parent = entry["uuid"]

    def test_reasoning_is_marked_and_error_flag_set(self) -> None:
        text = json.dumps(self.entries())
        self.assertIn(REASONING_PREFIX.strip().replace("\n", ""), text)
        results = [b for e in self.entries() if e["type"] == "user" for b in e["message"]["content"] if b["type"] == "tool_result"]  # type: ignore[index]
        self.assertTrue(results[1].get("is_error"))
        self.assertEqual(results[0]["content"], "file body")

    def test_written_log_counts_match_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "s.jsonl"
            log.write_text("".join(json.dumps(e) + "\n" for e in self.entries()), encoding="utf-8")
            self.assertEqual(count_written(log).content_tuple(), tally(sample_events()).content_tuple())

    def test_import_is_idempotent_and_verified(self) -> None:
        chat = ChatRef("abc-123", "My chat", 1000, None, False, "db", Path("/x"), "t", 7)
        with tempfile.TemporaryDirectory() as tmp:
            claude, sessions = Path(tmp) / "claude", Path(tmp) / "sessions"
            sessions.mkdir()
            first = import_chat(chat, sample_events, claude, sessions, True)
            second = import_chat(chat, sample_events, claude, sessions, True)
            self.assertEqual((first.status, first.verified), ("written", True))
            self.assertEqual(second.status, "exists")
            self.assertEqual(len(list(sessions.glob("local_*.json"))), 1)

    def test_helpers(self) -> None:
        self.assertEqual(safe_tool_name("mcp-memory memory.search"), "mcp-memory_memory_search")
        self.assertTrue(tool_use_id("c", sample_events()[2]).startswith("toolu_"))  # type: ignore[arg-type]
        self.assertEqual(session_uuid("x"), session_uuid("x"))
        self.assertEqual(key_range("bubbleId:a:"), ("bubbleId:a:", "bubbleId:a;"))
        self.assertEqual(parse_iso_ms("1970-01-01T00:00:01.500Z"), 1500)

    def test_resolve_cwd_maps_windows_path_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ExampleApp"
            target.mkdir()
            self.assertEqual(resolve_cwd("d:\\Work\\ExampleApp", {"exampleapp": str(target)}), str(target))
            self.assertEqual(resolve_cwd(None), str(Path.home()))


if __name__ == "__main__":
    unittest.main()
