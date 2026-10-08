"""Cursor agent conversation state (experimental 'carry model context'): codec, message shapes, safety."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from chatbridge.agent_state import blob_bytes, decode_state, encode_state, events_to_messages, message_roles
from chatbridge.config import Settings
from chatbridge.model import AssistantText, Event, Reasoning, ToolCall, UserText
from chatbridge.protobuf import field_bytes, field_varint, parse_fields
from chatbridge.sync import SyncService, SyncState
from tests.fixtures import CHAT_MAIN, CLAUDE_CLI, CLAUDE_KEY, World, add_native_agent_state, claude_follow_up
from tests.test_sync import composer_of, find, native_session


@pytest.fixture
def carry(world: World) -> SyncService:
    return SyncService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))], carry_context=True))


def state_of(world: World) -> str:
    return str(composer_of(world, "Refactor lexer").get("conversationState", ""))


def db(world: World) -> sqlite3.Connection:
    return sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")


# --------------------------------------------------------------------------- codec and message shapes
def test_protobuf_codec_roundtrip() -> None:
    body = field_bytes(1, b"\x01" * 32) + field_varint(5, 300) + field_bytes(3, b"abc") + field_varint(26, 1_788_275_300_088)
    assert parse_fields(body) == [(1, 2, b"\x01" * 32), (5, 0, 300), (3, 2, b"abc"), (26, 0, 1_788_275_300_088)]
    assert parse_fields(b"\x0a\xff") is None, "truncated length-delimited field is not valid"


def test_state_string_roundtrip() -> None:
    hashes = [hashlib.sha256(bytes([i])).digest() for i in range(5)]
    assert decode_state(encode_state(hashes, 1234, 256000, 1)) == hashes
    assert decode_state("~") is None and decode_state("") is None and decode_state("not a state") is None


def parts_of(message: dict[str, object]) -> list[dict[str, object]]:
    content = message["content"]
    assert isinstance(content, list)
    return [dict(p) for p in content]


def test_messages_pair_every_tool_call_with_a_result_and_skip_reasoning() -> None:
    events: list[Event] = [
        UserText("fix it", 1_788_000_000_000),
        Reasoning("thinking", 2),
        AssistantText("looking", 3),
        ToolCall(0, "a", "Read", {"file_path": "/x"}, "body", False, 4),
        ToolCall(1, "b", "Bash", {"command": "false"}, None, True, 5),
        AssistantText("done", 6),
        UserText("thanks", 7),
    ]
    messages = events_to_messages(events)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "assistant", "user"]
    assert str(parts_of(messages[0])[0]["text"]).endswith("<user_query>\nfix it\n</user_query>")
    assert all(p.get("type") != "reasoning" for m in messages for p in parts_of(m))
    calls = [p["toolCallId"] for p in parts_of(messages[1]) if p["type"] == "tool-call"]
    assert len(calls) == 2 and [m["id"] for m in messages[2:4]] == calls
    assert json.dumps(messages[3]).count('"isError": true') == 1, "the failed call is flagged as an error"
    assert parts_of(messages[1])[0] == {"type": "text", "text": "looking"}


# --------------------------------------------------------------------------- integration
def test_new_chat_gets_model_context_with_borrowed_prefix_and_undo_removes_it(carry: SyncService, world: World) -> None:
    prefix_hashes, _ = add_native_agent_state(world.live_user / "globalStorage" / "state.vscdb", CHAT_MAIN)
    native_session(world)
    report = carry.sync(find(carry, claude_key=CLAUDE_KEY), apply=True)
    assert report.status == "synced" and not report.notes

    state = state_of(world)
    hashes = decode_state(state)
    assert hashes is not None and hashes[:2] == prefix_hashes[:2], "system prompt and environment message are borrowed from the native chat"
    conn = db(world)
    for digest in hashes:
        (blob,) = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"agentKv:blob:{digest.hex()}",)).fetchone()
        assert hashlib.sha256(blob).digest() == digest, "blobs are content-addressed"
    roles = message_roles(conn, state)
    assert roles[:2] == ["system", "user"] and roles[2] == "user" and "assistant" in roles and roles.count("tool") == 2
    blob_count = conn.execute("SELECT count(*) FROM cursorDiskKV WHERE key LIKE 'agentKv:blob:%'").fetchone()[0]
    conn.close()

    carry.undo_cursor_write(Path(report.journal))
    conn = db(world)
    assert conn.execute("SELECT count(*) FROM cursorDiskKV WHERE key LIKE 'agentKv:blob:%'").fetchone()[0] == blob_count - (blob_count - 3)
    conn.close()
    for digest in prefix_hashes:
        conn = db(world)
        assert conn.execute("SELECT 1 FROM cursorDiskKV WHERE key = ?", (f"agentKv:blob:{digest.hex()}",)).fetchone() is not None, (
            "native blobs untouched"
        )
        conn.close()


def test_off_by_default_writes_no_agent_state(world: World) -> None:
    add_native_agent_state(world.live_user / "globalStorage" / "state.vscdb", CHAT_MAIN)
    sync = SyncService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))]))
    native_session(world)
    sync.sync(find(sync, claude_key=CLAUDE_KEY), apply=True)
    assert decode_state(state_of(world)) is None


def test_without_a_native_chat_the_feature_is_skipped_with_a_note(carry: SyncService, world: World) -> None:
    native_session(world)  # the fixture's chats have no agent state to borrow a system prompt from
    report = carry.sync(find(carry, claude_key=CLAUDE_KEY), apply=True)
    assert report.status == "synced" and any("not carried" in n for n in report.notes)
    assert decode_state(state_of(world)) is None, "the visible chat is still created"


def test_follow_up_extends_the_state_and_keeps_the_prefix(carry: SyncService, world: World) -> None:
    add_native_agent_state(world.live_user / "globalStorage" / "state.vscdb", CHAT_MAIN)
    native_session(world)
    carry.sync(find(carry, claude_key=CLAUDE_KEY), apply=True)
    before = decode_state(state_of(world)) or []
    claude_follow_up(world, CLAUDE_CLI, [("u", "now add docs"), ("a", "docs added")])
    report = carry.sync(find(carry, claude_key=CLAUDE_KEY), apply=True)
    assert report.status == "synced"
    after = decode_state(state_of(world)) or []
    assert after[: len(before)] == before and len(after) == len(before) + 2
    assert message_roles(db(world), state_of(world))[-2:] == ["user", "assistant"]


def test_existing_chat_is_repaired_when_the_setting_is_turned_on_later(world: World) -> None:
    add_native_agent_state(world.live_user / "globalStorage" / "state.vscdb", CHAT_MAIN)
    plain = SyncService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))]))
    native_session(world)
    plain.sync(find(plain, claude_key=CLAUDE_KEY), apply=True)
    assert decode_state(state_of(world)) is None

    carry = SyncService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))], carry_context=True))
    conv = find(carry, claude_key=CLAUDE_KEY)
    assert conv.state is SyncState.IN_SYNC
    dry = carry.sync(conv, apply=False)
    assert any("conversation as context" in f for f in dry.fixes)
    assert decode_state(state_of(world)) is None, "a dry run must not write"
    report = carry.sync(conv, apply=True)
    assert report.status == "synced" and decode_state(state_of(world)) is not None
    assert carry.sync(find(carry, claude_key=CLAUDE_KEY), apply=True).status == "noop", "idempotent"
    carry.undo_cursor_write(Path(report.journal))
    assert decode_state(state_of(world)) is None


def test_native_cursor_chats_are_never_given_synthetic_state(carry: SyncService, world: World) -> None:
    """A chat that originated in Cursor keeps Cursor's own agent state even when Claude follow-ups are appended."""
    prefix_hashes, native_state = add_native_agent_state(world.live_user / "globalStorage" / "state.vscdb", CHAT_MAIN)
    carry.sync(find(carry, cursor_id=CHAT_MAIN), apply=True)
    paired = find(carry, cursor_id=CHAT_MAIN)
    assert paired.claude is not None
    claude_follow_up(world, paired.claude.cli_id, [("u", "follow up in claude"), ("a", "reply")])
    assert carry.sync(find(carry, cursor_id=CHAT_MAIN), apply=True).status == "synced"
    conn = db(world)
    (value,) = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{CHAT_MAIN}",)).fetchone()
    conn.close()
    assert json.loads(value)["conversationState"] == native_state
    assert len(prefix_hashes) == 3


def test_blob_bytes_are_compact_utf8_json() -> None:
    assert blob_bytes({"role": "user", "content": "héllo"}) == '{"role":"user","content":"héllo"}'.encode()


# --------------------------------------------------------------------------- context budget
def test_trim_keeps_recent_whole_turns_starting_with_a_user_message() -> None:
    from chatbridge.agent_state import trim_to_budget

    events: list[Event] = []
    for turn in range(10):
        events += [
            UserText(f"question {turn}", turn),
            ToolCall(0, f"c{turn}", "Read", {"p": turn}, "o" * 4000, False, turn),
            AssistantText(f"answer {turn}", turn),
        ]
    messages = events_to_messages(events)
    kept, dropped = trim_to_budget(messages, 3000)
    assert dropped > 0 and kept[0]["role"] == "user" and kept == messages[dropped:]
    assert "question 9" in json.dumps(kept) and "question 0" not in json.dumps(kept)
    ids = {p["toolCallId"] for m in kept if m["role"] == "assistant" for p in parts_of(m) if p["type"] == "tool-call"}
    assert {m["id"] for m in kept if m["role"] == "tool"} <= ids, "no tool result without its tool call"
    assert trim_to_budget(messages, 10**9) == (messages, 0)


def test_huge_conversations_are_trimmed_with_a_note_and_huge_outputs_are_capped(world: World) -> None:
    from chatbridge.agent_state import MAX_TOOL_OUTPUT_CHARS

    big = SyncService(world.paths, Settings(carry_context=True))
    add_native_agent_state(world.live_user / "globalStorage" / "state.vscdb", CHAT_MAIN)
    log = __import__("tests.fixtures", fromlist=["ClaudeLog"]).ClaudeLog(CLAUDE_CLI, str(world.project_dir))
    for turn in range(40):
        log.user(f"question {turn}")
        log.assistant(f"answer {turn}", tool=("Read", {"file_path": f"/f{turn}"}, "x" * 60_000, False))
    from tests.fixtures import write_claude_session

    write_claude_session(world, CLAUDE_KEY, CLAUDE_CLI, "Huge chat", log, str(world.project_dir))
    report = big.sync(find(big, claude_key=CLAUDE_KEY), apply=True)
    assert report.status == "synced"
    state = str(composer_of(world, "Huge chat")["conversationState"])
    conn = db(world)
    texts = []
    for digest in decode_state(state) or []:
        (blob,) = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"agentKv:blob:{digest.hex()}",)).fetchone()
        texts.append(blob.decode())
    conn.close()
    joined = "\n".join(texts)
    assert "question 39" in joined and "question 0" not in joined.split("[ChatBridge]")[-1]
    assert "[ChatBridge] The first" in joined, "the model is told that earlier messages were left out"
    assert "characters truncated by ChatBridge" in joined
    assert all(len(t) < 3 * MAX_TOOL_OUTPUT_CHARS + 6000 for t in texts[2:]), (
        "each output is capped (Cursor stores it three times per message)"
    )
    used = int(composer_of(world, "Huge chat")["contextTokensUsed"])  # type: ignore[call-overload]
    assert used <= 256000 // 2 + 5000, "the meter reflects the trimmed state, within budget"
