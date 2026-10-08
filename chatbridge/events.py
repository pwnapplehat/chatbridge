"""Tool-neutral conversation events: identity keys, timestamps and multiset merge planning.

Both Cursor and Claude chats are read into the same Event stream. Two events are "the same" when their
identity key matches; merging two chats is then a pure multiset operation (no stored baseline needed,
safe to repeat, never deletes anything).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from .converter import ATTACHMENT_PREFIX, OPENER_PREFIX
from .model import AssistantText, EmptyRecord, Event, Reasoning, ToolCall, UserText

MCP_CLAUDE_PREFIX = "mcp-claude-"
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
HARNESS_PREFIXES = ("<command-name>", "<local-command-", "<command-message>", "Caveat: The messages below were generated")


def normalize_user_text(text: str) -> str:
    """User text as it survives a trip through Claude: harness injections removed; '' if nothing human remains.

    The same function is used when reading Claude logs and when computing identity keys, so a message that
    exists in Cursor but is never visible on the Claude side can never cause endless re-appending.
    """
    cleaned = SYSTEM_REMINDER_RE.sub("", text).strip()
    return "" if cleaned.startswith((ATTACHMENT_PREFIX, OPENER_PREFIX, *HARNESS_PREFIXES)) else cleaned


def event_ts(event: Event) -> int:
    """Timestamp (epoch ms) of an event; 0 for empty records."""
    return 0 if isinstance(event, EmptyRecord) else event.ts_ms


def canonical_tool_name(name: str) -> str:
    """Tool name as the tools themselves know it (Claude-origin tools are stored in Cursor as mcp-claude-<name>)."""
    return name[len(MCP_CLAUDE_PREFIX) :] if name.startswith(MCP_CLAUDE_PREFIX) else name


def event_key(event: Event) -> str | None:
    """Content identity of an event, stable across a round trip through either tool. None for empty records."""
    body: tuple[str, ...]
    if isinstance(event, UserText):
        body = ("u", normalize_user_text(event.text))
    elif isinstance(event, AssistantText):
        body = ("a", event.text.strip())
    elif isinstance(event, Reasoning):
        body = ("r", event.text.strip())
    elif isinstance(event, ToolCall):
        body = ("t", canonical_tool_name(event.name), json.dumps(event.tool_input, sort_keys=True, ensure_ascii=False))
    else:
        return None
    return hashlib.sha1(json.dumps(body, ensure_ascii=False).encode()).hexdigest()


def meaningful(events: Iterable[Event]) -> list[Event]:
    """Events that carry content (drops empty records and whitespace-only user/assistant text)."""
    out: list[Event] = []
    for event in events:
        if isinstance(event, EmptyRecord):
            continue
        if isinstance(event, UserText):
            if normalize_user_text(event.text):
                out.append(event)
            continue
        if isinstance(event, (AssistantText, Reasoning)) and not event.text.strip():
            continue
        out.append(event)
    return out


def missing_events(source: list[Event], destination: list[Event]) -> list[Event]:
    """Events of `source` that `destination` lacks, as a multiset difference (later duplicates are the 'extra' ones).

    Example: source has "ok" three times, destination once -> the last two "ok" events are returned.
    """
    have = Counter(k for e in destination if (k := event_key(e)) is not None)
    seen: defaultdict[str, int] = defaultdict(int)
    missing: list[Event] = []
    for event in source:
        key = event_key(event)
        if key is None:
            continue
        seen[key] += 1
        if seen[key] > have[key]:
            missing.append(event)
    return missing
