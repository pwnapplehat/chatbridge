"""Cursor's agent conversation state: what the model is sent when you continue a chat (EXPERIMENTAL).

Cursor keeps the model-facing history of a chat as a list of content-addressed JSON messages in
``agentKv:blob:<sha256>`` rows (roles ``system``, ``user``, ``assistant``, ``tool``, AI-SDK style parts), and
``composerData.conversationState`` as ``"~" + base64(protobuf)``: field 1 repeats the 32-byte hashes of those messages
in order, plus a few metadata fields. The visible chat (bubbles) is only a view; without this state the model starts blank.

ChatBridge rebuilds that state from the events of an imported conversation. The static prefix (system prompt and
environment message) is borrowed from the profile's most recent native chat. Reasoning is not included (Cursor's
reasoning parts are provider-signed and cannot be forged).
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .model import AssistantText, Event, JsonObj, ToolCall, UserText, as_obj, as_str
from .osenv import local_moment
from .protobuf import field_bytes, field_varint, parse_fields

BLOB_KEY = "agentKv:blob:"
COMPOSER_KEY = "composerData:"
MAX_TOOL_OUTPUT_CHARS = 20_000
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class StateBuild:
    """A conversation state ready to be stored: new blobs (hash hex -> bytes) and the composerData string."""

    blobs: dict[str, bytes]
    state: str
    tokens: int


def blob_bytes(message: JsonObj) -> bytes:
    """Canonical blob encoding (compact JSON, UTF-8); its sha256 is the blob key."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_state(state: str) -> list[bytes] | None:
    """Field-1 hashes of a conversationState string, or None if it is empty ('~') or not a state."""
    if not state.startswith("~") or len(state) <= 1:
        return None
    try:
        raw = base64.b64decode(state[1:] + "=" * (-len(state[1:]) % 4))
    except ValueError:
        return None
    fields = parse_fields(raw)
    if fields is None:
        return None
    return [v for number, wire, v in fields if number == 1 and wire == 2 and isinstance(v, bytes) and len(v) == 32]


def encode_state(hashes: list[bytes], tokens: int, limit: int, now_ms: int) -> str:
    """conversationState string: message hash list (field 1), token usage (5), format version (10), timestamp (26)."""
    body = b"".join(field_bytes(1, h) for h in hashes)
    body += field_bytes(5, field_varint(1, tokens) + field_varint(2, limit)) + field_varint(10, 1) + field_varint(26, now_ms)
    return "~" + base64.b64encode(body).decode("ascii")


def _read_blob(conn: sqlite3.Connection, digest: bytes) -> bytes | None:
    row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (BLOB_KEY + digest.hex(),)).fetchone()
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def find_prefix(conn: sqlite3.Connection, own_ids: set[str] | None = None) -> list[bytes] | None:
    """[system blob, environment blob] taken from the profile's most recent native chat, or None if there is none."""
    own = own_ids or set()
    rows = conn.execute("SELECT composerId FROM composerHeaders WHERE isSubagent = 0 ORDER BY recency DESC LIMIT 60").fetchall()
    for (composer_id,) in rows:
        if composer_id in own:
            continue
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_KEY + composer_id,)).fetchone()
        if row is None:
            continue
        try:
            state = as_str(as_obj(json.loads(row[0])).get("conversationState"))
        except json.JSONDecodeError:
            continue
        hashes = decode_state(state)
        if not hashes or len(hashes) < 2:
            continue
        first, second = _read_blob(conn, hashes[0]), _read_blob(conn, hashes[1])
        if first is None or second is None:
            continue
        try:
            if as_str(as_obj(json.loads(first)).get("role")) == "system" and as_str(as_obj(json.loads(second)).get("role")) == "user":
                return [first, second]
        except json.JSONDecodeError:
            continue
    return None


def _stamp(ts_ms: int) -> str:
    moment = local_moment(ts_ms)
    offset = moment.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    sign, minutes = ("+" if minutes >= 0 else "-"), abs(minutes)
    zone = f"UTC{sign}{minutes // 60}" + (f":{minutes % 60:02d}" if minutes % 60 else "")
    return f"{moment.strftime('%A, %b')} {moment.day}, {moment.year}, {moment.strftime('%I:%M %p').lstrip('0')} ({zone})"


def events_to_messages(events: list[Event]) -> list[JsonObj]:
    """AI-SDK style messages for the model from conversation events (reasoning is skipped)."""
    messages: list[JsonObj] = []
    parts: list[JsonObj] = []
    results: list[JsonObj] = []

    def flush() -> None:
        nonlocal parts, results
        if parts:
            message_id = f"msg_{uuid.uuid4()}"
            messages.append(
                {
                    "role": "assistant",
                    "content": parts,
                    "id": message_id,
                    "providerOptions": {"cursor": {"modelProviderMessageId": message_id}},
                }
            )
            messages.extend(results)
        parts, results = [], []

    for event in events:
        if isinstance(event, UserText):
            if not event.text.strip():
                continue
            flush()
            text = f"<timestamp>{_stamp(event.ts_ms)}</timestamp>\n<user_query>\n{event.text}\n</user_query>"
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "providerOptions": {"cursor": {"requestId": str(uuid.uuid4())}},
                }
            )
        elif isinstance(event, AssistantText):
            if event.text.strip():
                if results:
                    flush()
                parts.append({"type": "text", "text": event.text})
        elif isinstance(event, ToolCall):
            call_id = f"call-{uuid.uuid4()}-{event.seq}"
            parts.append({"type": "tool-call", "toolCallId": call_id, "toolName": event.name, "args": event.tool_input})
            output = event.output if event.output is not None else ""
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                output = (
                    output[:MAX_TOOL_OUTPUT_CHARS]
                    + f"\n[... {len(event.output or '') - MAX_TOOL_OUTPUT_CHARS:,} characters truncated by ChatBridge]"
                )
            results.append(
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool-result", "toolCallId": call_id, "toolName": event.name, "result": output,
                            "experimental_content": [{"type": "text", "text": output}],
                        }
                    ],
                    "id": call_id,
                    "providerOptions": {"cursor": {"highLevelToolCallResult": {"output": {"success": {"content": output}}, "isError": event.is_error}}},
                }
            )  # fmt: skip
    flush()
    return messages


def trim_to_budget(messages: list[JsonObj], budget_tokens: int) -> tuple[list[JsonObj], int]:
    """Keep the most recent whole turns that fit `budget_tokens`; returns (kept, number dropped).

    The kept list always starts with a user message, so no tool result is left without its tool call.
    """
    sizes = [len(blob_bytes(m)) // CHARS_PER_TOKEN for m in messages]
    if sum(sizes) <= budget_tokens:
        return messages, 0
    start, running = len(messages), 0
    for index in range(len(messages) - 1, -1, -1):
        running += sizes[index]
        if running > budget_tokens:
            break
        start = index
    while start < len(messages) and messages[start]["role"] != "user":
        start += 1
    if start >= len(messages):
        users = [i for i, m in enumerate(messages) if m["role"] == "user"]
        start = users[-1] if users else 0
    return messages[start:], start


def _omission_note(dropped: int) -> JsonObj:
    text = (
        f"<user_query>\n[ChatBridge] The first {dropped} messages of this imported conversation are not included in your context "
        "because it is too long. They remain visible in the chat history; ask the user if you need something from them.\n</user_query>"
    )
    return {"role": "user", "content": [{"type": "text", "text": text}], "providerOptions": {"cursor": {"requestId": str(uuid.uuid4())}}}


def build_state(
    prefix: list[bytes],
    existing: list[bytes] | None,
    events: list[Event],
    limit: int,
    token_estimate: int,
    budget_tokens: int | None = None,
) -> StateBuild:
    """State = (existing hashes, or the borrowed prefix) + the messages of `events`. New message blobs are returned.

    With `budget_tokens`, only the most recent turns that fit are included, preceded by a note about what was left out.
    """
    blobs: dict[str, bytes] = {}
    hashes: list[bytes] = list(existing) if existing else [hashlib.sha256(b).digest() for b in prefix]
    if not existing:
        blobs.update({hashlib.sha256(b).hexdigest(): b for b in prefix})
    all_messages = events_to_messages(events)
    if budget_tokens is not None:
        kept, dropped = trim_to_budget(all_messages, budget_tokens)
        all_messages = ([_omission_note(dropped)] if dropped else []) + kept
        token_estimate = sum(len(blob_bytes(m)) for m in all_messages) // CHARS_PER_TOKEN
    for message in all_messages:
        data = blob_bytes(message)
        digest = hashlib.sha256(data).digest()
        blobs[digest.hex()] = data
        hashes.append(digest)
    prefix_tokens = sum(len(b) for b in prefix) // 4
    tokens = token_estimate + (0 if existing else prefix_tokens)
    return StateBuild(blobs, encode_state(hashes, tokens, limit, int(time.time() * 1000)), tokens)


def message_roles(conn: sqlite3.Connection, state: str) -> list[str]:
    """Roles of the messages a state lists (for tests and diagnostics)."""
    roles: list[str] = []
    for digest in decode_state(state) or []:
        data = _read_blob(conn, digest)
        roles.append(as_str(as_obj(json.loads(data)).get("role")) if data else "?")
    return roles
