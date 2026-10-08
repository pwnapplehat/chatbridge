"""Convert a stream of Cursor events into Claude Code session-log entries.

Cursor tool calls become real tool_use / tool_result pairs (Claude requires every
tool_use to be answered by a tool_result in the next user entry). Cursor reasoning is kept as
text marked '[Cursor reasoning]'. User text is preserved verbatim.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .model import AssistantText, Counts, EmptyRecord, Event, JsonObj, Reasoning, ToolCall, UserText

CLAUDE_VERSION = "2.1.284"
SYNTHETIC_MODEL = "<synthetic>"
REASONING_PREFIX = "[Cursor reasoning]\n"
ATTACHMENT_PREFIX = "[Cursor attachment:"
OPENER_PREFIX = "[Cursor: no first user message was stored for this chat"
NO_OUTPUT_TEXT = "(Cursor did not store an output for this tool call)"
NO_ERROR_DETAIL_TEXT = "(Cursor reported an error for this tool call; no details were stored)"
TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def iso_ms(epoch_ms: int) -> str:
    """Format epoch milliseconds as Claude's ISO-8601 UTC timestamp."""
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"


def tool_use_id(chat_id: str, call: ToolCall) -> str:
    """Deterministic, API-valid tool_use id for a Cursor call."""
    digest = hashlib.sha1(f"{chat_id}:{call.seq}:{call.call_id}".encode()).hexdigest()[:24]
    return f"toolu_{digest}"


def safe_tool_name(name: str) -> str:
    """Restrict tool names to the characters the API accepts."""
    return TOOL_NAME_RE.sub("_", name)[:64] or "unknown_tool"


def derive_title(name: str, first_user_text: str) -> str:
    """Session title: Cursor's chat name, else the first words the user typed."""
    base = name.strip() or " ".join(first_user_text.split())[:60] or "Imported chat"
    return f"[Cursor] {base}"


@dataclass
class _Builder:
    """Mutable state while turning events into entries."""

    session_id: str
    chat_id: str
    cwd: str
    parent: str | None = None
    last_ts: int = 0
    prompt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    blocks: list[JsonObj] = field(default_factory=list)
    results: list[JsonObj] = field(default_factory=list)
    out: list[JsonObj] = field(default_factory=list)
    last_user_text: str = ""

    def _entry(self, kind: str, ts_ms: int, message: JsonObj, extra: JsonObj) -> JsonObj:
        self.last_ts = max(self.last_ts, ts_ms)
        node = str(uuid.uuid4())
        entry: JsonObj = {
            "parentUuid": self.parent,
            "isSidechain": False,
            **extra,
            "type": kind,
            "message": message,
            "uuid": node,
            "timestamp": iso_ms(self.last_ts),
            "userType": "external",
            "entrypoint": "claude-desktop",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": CLAUDE_VERSION,
            "gitBranch": "HEAD",
        }
        self.parent = node
        self.out.append(entry)
        return entry

    def flush(self, ts_ms: int) -> None:
        """Emit the pending assistant message and, if it called tools, the matching results."""
        if not self.blocks:
            return
        has_tools = bool(self.results)
        message: JsonObj = {
            "model": SYNTHETIC_MODEL,
            "id": "msg_" + uuid.uuid4().hex[:24],
            "type": "message",
            "role": "assistant",
            "content": self.blocks,
            "stop_reason": "tool_use" if has_tools else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self._entry("assistant", ts_ms, message, {})
        if has_tools:
            self._entry("user", ts_ms, {"role": "user", "content": self.results}, {"promptId": self.prompt_id})
        self.blocks, self.results = [], []

    def ensure_opening_user(self, ts_ms: int) -> None:
        """Claude conversations must start with a user message; subagent runs never stored theirs, so say so honestly."""
        if self.parent is not None:
            return
        note = f"{OPENER_PREFIX} (for example a subagent run started by another chat). The conversation below is what Cursor recorded.]"
        self._entry(
            "user",
            ts_ms,
            {"role": "user", "content": [{"type": "text", "text": note}]},
            {"promptId": self.prompt_id, "permissionMode": "default", "origin": {"kind": "human"}},
        )

    def add_text(self, text: str, ts_ms: int) -> None:
        """Append a text block, closing any tool round-trip first."""
        self.ensure_opening_user(ts_ms)
        if self.results:
            self.flush(ts_ms)
        self.blocks.append({"type": "text", "text": text})

    def add_tool(self, call: ToolCall) -> None:
        """Append a tool_use block and queue its tool_result."""
        self.ensure_opening_user(call.ts_ms)
        use_id = tool_use_id(self.chat_id, call)
        self.blocks.append({"type": "tool_use", "id": use_id, "name": safe_tool_name(call.name), "input": call.tool_input})
        stored = call.output if call.output else None
        content = stored if stored is not None else (NO_ERROR_DETAIL_TEXT if call.is_error else NO_OUTPUT_TEXT)
        result: JsonObj = {"type": "tool_result", "tool_use_id": use_id, "content": content}
        if call.is_error:
            result["is_error"] = True
        self.results.append(result)

    def add_user(self, event: UserText) -> None:
        """Emit a human message (flushing any pending assistant output first)."""
        self.flush(event.ts_ms)
        self.prompt_id = str(uuid.uuid4())
        self.last_user_text = event.text
        content: list[JsonObj] = []
        if event.text.strip():
            content.append({"type": "text", "text": event.text})
        if event.image_count:
            note = f"{ATTACHMENT_PREFIX} {event.image_count} image(s) were attached in Cursor and are not imported]"
            content.append({"type": "text", "text": note})
        self._entry(
            "user",
            event.ts_ms,
            {"role": "user", "content": content},
            {"promptId": self.prompt_id, "permissionMode": "default", "origin": {"kind": "human"}},
        )


def count_event(counts: Counts, event: Event) -> Counts:
    """Return counts updated for one source event."""
    if isinstance(event, UserText):
        return Counts(
            counts.user + 1,
            counts.assistant_text,
            counts.reasoning,
            counts.tool_calls,
            counts.tool_results,
            counts.text_chars + len(event.text),
            counts.empty_records,
        )
    if isinstance(event, AssistantText):
        return Counts(
            counts.user,
            counts.assistant_text + 1,
            counts.reasoning,
            counts.tool_calls,
            counts.tool_results,
            counts.text_chars + len(event.text),
            counts.empty_records,
        )
    if isinstance(event, Reasoning):
        return Counts(
            counts.user,
            counts.assistant_text,
            counts.reasoning + 1,
            counts.tool_calls,
            counts.tool_results,
            counts.text_chars + len(event.text),
            counts.empty_records,
        )
    if isinstance(event, ToolCall):
        stored = len(event.output) if event.output else 0
        return Counts(
            counts.user,
            counts.assistant_text,
            counts.reasoning,
            counts.tool_calls + 1,
            counts.tool_results + 1,
            counts.text_chars + stored,
            counts.empty_records,
        )
    return Counts(
        counts.user,
        counts.assistant_text,
        counts.reasoning,
        counts.tool_calls,
        counts.tool_results,
        counts.text_chars,
        counts.empty_records + 1,
    )


def convert(events: Iterable[Event], session_id: str, chat_id: str, cwd: str, title: str) -> Iterator[JsonObj]:
    """Yield Claude log entries for a stream of events, followed by the title/prompt trailer lines.

    Entries are yielded in batches as tool round-trips complete, so memory stays bounded
    even for chats with hundreds of thousands of records.
    """
    state = _Builder(session_id, chat_id, cwd)
    for event in events:
        if isinstance(event, UserText):
            state.add_user(event)
        elif isinstance(event, AssistantText):
            state.add_text(event.text, event.ts_ms)
        elif isinstance(event, Reasoning):
            state.add_text(REASONING_PREFIX + event.text, event.ts_ms)
        elif isinstance(event, ToolCall):
            state.add_tool(event)
            state.last_ts = max(state.last_ts, event.ts_ms)
        elif isinstance(event, EmptyRecord):
            continue
        if len(state.out) >= 256:
            yield from state.out
            state.out.clear()
    state.flush(state.last_ts)
    yield from state.out
    yield {"type": "custom-title", "customTitle": title, "sessionId": session_id}
    yield {"type": "agent-name", "agentName": title, "sessionId": session_id}
    yield {"type": "last-prompt", "lastPrompt": state.last_user_text[:200], "leafUuid": state.parent, "sessionId": session_id}


def build_continuation(
    events: Iterable[Event], session_id: str, chat_id: str, cwd: str, parent_uuid: str | None, last_ts_ms: int
) -> tuple[list[JsonObj], str | None, str]:
    """Entries that continue an existing log after `parent_uuid` (no title/prompt trailer).

    Returns (entries, uuid of the last entry or the given parent, last user text). Timestamps never go backwards
    relative to `last_ts_ms`, matching what Claude expects inside one log.
    """
    state = _Builder(session_id, chat_id, cwd, parent=parent_uuid, last_ts=last_ts_ms)
    for event in events:
        if isinstance(event, UserText):
            state.add_user(event)
        elif isinstance(event, AssistantText):
            state.add_text(event.text, event.ts_ms)
        elif isinstance(event, Reasoning):
            state.add_text(REASONING_PREFIX + event.text, event.ts_ms)
        elif isinstance(event, ToolCall):
            state.add_tool(event)
            state.last_ts = max(state.last_ts, event.ts_ms)
    state.flush(state.last_ts)
    return state.out, state.parent, state.last_user_text
