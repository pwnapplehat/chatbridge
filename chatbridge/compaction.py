"""Keep imported Claude sessions inside the model's context window by compacting them the way Claude Code does.

Claude's log IS the model context: a conversation chain that is far larger than the window (a 17,000-record Cursor chat is
about 18.8 million tokens) cannot be continued and cannot even be /compact-ed. So a big import keeps the full history in the
log (before a ``compact_boundary``) and the active chain after the boundary is: a summary of the older part (marked
``isCompactSummary``, same wrapper text Claude Code writes) followed by copies of the most recent turns (marked
``chatbridgeRecap`` so syncing never counts them twice, with tool outputs capped).

The summary is extractive and deterministic (no model call): the user's requests in order plus the last replies before the cut.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .model import AssistantText, Event, Reasoning, ToolCall, UserText
from .osenv import local_moment

CHARS_PER_TOKEN = 4
FULL_BUDGET_TOKENS = 120_000
RECENT_BUDGET_TOKENS = 40_000
SUMMARY_MAX_CHARS = 48_000
REQUEST_MAX_CHARS = 700
REPLY_MAX_CHARS = 1_200
MAX_OUTPUT_CHARS = 20_000
KEEP_FIRST_REQUESTS = 3
WRAPPER_HEAD = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation.\n\n"
)
WRAPPER_TRANSCRIPT = (
    "\n\nIf you need specific details from before compaction (like exact code snippets, error messages, or content you "
    "generated), read the full transcript at: {path}"
)
WRAPPER_RECENT = "\n\nRecent messages are preserved verbatim."
WRAPPER_HEAD_TRUNCATED = (
    "\n\nNote: the earliest part of the conversation was too large to include and is NOT covered by this summary "
    "(the full transcript mentioned above still has it). If the task turns out to depend on something from that part, "
    "say so plainly rather than guessing at it."
)


def capped_output(output: str | None) -> str | None:
    """Tool output limited to MAX_OUTPUT_CHARS (with a marker) for the copies that enter the model's context."""
    if output is None or len(output) <= MAX_OUTPUT_CHARS:
        return output
    return (
        output[:MAX_OUTPUT_CHARS]
        + f"\n[... {len(output) - MAX_OUTPUT_CHARS:,} characters truncated by ChatBridge; the full output is earlier in the session log]"
    )


def event_tokens(event: Event) -> int:
    """Rough token size of an event as it would enter the model's context (outputs counted at their capped size)."""
    if isinstance(event, (UserText, AssistantText, Reasoning)):
        return len(event.text) // CHARS_PER_TOKEN
    if isinstance(event, ToolCall):
        output = capped_output(event.output) or ""
        return (len(json.dumps(event.tool_input, ensure_ascii=False)) + len(output) + 40) // CHARS_PER_TOKEN
    return 0


@dataclass
class EventScan:
    """What the planner needs from one pass over a conversation (kept small: no full texts are retained)."""

    sizes: list[int] = field(default_factory=list)
    requests: list[tuple[int, int, str]] = field(default_factory=list)  # (event index, ts_ms, abridged text)
    replies: list[tuple[int, str]] = field(default_factory=list)  # (event index, abridged text) of the latest assistant texts
    first_ts: int = 0
    last_ts: int = 0

    def add(self, event: Event) -> None:
        index = len(self.sizes)
        self.sizes.append(event_tokens(event))
        ts = getattr(event, "ts_ms", 0)
        self.first_ts = self.first_ts or ts
        self.last_ts = max(self.last_ts, ts)
        if isinstance(event, UserText) and event.text.strip():
            self.requests.append((index, ts, " ".join(event.text.split())[:REQUEST_MAX_CHARS]))
        elif isinstance(event, AssistantText) and event.text.strip():
            self.replies.append((index, " ".join(event.text.split())[:REPLY_MAX_CHARS]))
            if len(self.replies) > 12:
                self.replies.pop(0)


@dataclass(frozen=True)
class CompactionPlan:
    """Where to cut and what the summary says."""

    recent_start: int
    summary: str
    omitted: int
    recent: int
    pre_tokens: int
    post_tokens: int
    truncated_requests: bool


def _day(ts_ms: int) -> str:
    return local_moment(ts_ms).strftime("%Y-%m-%d") if ts_ms else "?"


def plan_compaction(scan: EventScan, title: str, source: str) -> CompactionPlan | None:
    """None when the conversation already fits; otherwise the cut index and summary text."""
    total = sum(scan.sizes)
    if total <= FULL_BUDGET_TOKENS or not scan.requests:
        return None
    user_indexes = [i for i, _, _ in scan.requests]
    running, boundary = 0, len(scan.sizes)
    for index in range(len(scan.sizes) - 1, -1, -1):
        running += scan.sizes[index]
        if running > RECENT_BUDGET_TOKENS:
            break
        boundary = index
    later = [i for i in user_indexes if i >= boundary]
    start = later[0] if later else user_indexes[-1]
    older = [(i, ts, text) for i, ts, text in scan.requests if i < start]
    if not older:
        return None
    summary, truncated = _summary(scan, older, start, title, source)
    recent_tokens = sum(scan.sizes[start:])
    return CompactionPlan(start, summary, start, len(scan.sizes) - start, total, recent_tokens + len(summary) // CHARS_PER_TOKEN, truncated)


def _summary(scan: EventScan, older: list[tuple[int, int, str]], start: int, title: str, source: str) -> tuple[str, bool]:
    header = (
        f'Imported conversation "{title}" (from {source}). The first {start:,} of {len(scan.sizes):,} recorded messages, '
        f"{_day(scan.first_ts)} to {_day(older[-1][1])}, are summarized below; the most recent messages follow verbatim.\n\n"
        "The user's requests in the summarized part, in order (abridged):\n"
    )
    replies = [text for i, text in scan.replies if i < start][-3:]
    tail = ""
    if replies:
        tail = "\n\nLast assistant replies before the cut (abridged):\n" + "\n".join(f"- {t}" for t in replies)
    lines = [f"{n}. [{_day(ts)}] {text}" for n, (_, ts, text) in enumerate(older, start=1)]
    room = SUMMARY_MAX_CHARS - len(header) - len(tail) - 200
    if sum(len(x) + 1 for x in lines) <= room:
        return header + "\n".join(lines) + tail, False
    head = lines[:KEEP_FIRST_REQUESTS]
    kept_tail: list[str] = []
    used = sum(len(x) + 1 for x in head)
    for line in reversed(lines[KEEP_FIRST_REQUESTS:]):
        if used + len(line) + 1 > room:
            break
        kept_tail.insert(0, line)
        used += len(line) + 1
    skipped = len(lines) - len(head) - len(kept_tail)
    omitted = [f"[... {skipped:,} earlier requests omitted ...]"] if skipped else []
    return header + "\n".join(head + omitted + kept_tail) + tail, skipped > 0


def wrap_summary(plan: CompactionPlan, transcript_path: str) -> str:
    """The text of the compaction-summary user message, in Claude Code's own wrapper."""
    text = WRAPPER_HEAD + plan.summary + WRAPPER_TRANSCRIPT.format(path=transcript_path) + WRAPPER_RECENT
    return text + (WRAPPER_HEAD_TRUNCATED if plan.truncated_requests else "")
