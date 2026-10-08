"""Read Claude (desktop app / Claude Code) sessions into tool-neutral events.

A Claude session = a conversation log ``~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`` and, for desktop-app
sessions, a sidebar record ``claude-code-sessions/<org>/<account>/local_<uuid>.json`` whose ``cliSessionId`` names the
*current* log (the app re-keys it when a session is continued, copying the history into a new log).

Harness noise is not conversation: sidechain (subagent) entries, meta entries, <system-reminder> blocks, compaction
summaries and API-error stubs are skipped. Placeholder notes written by this tool are skipped too.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppPaths
from .converter import NO_ERROR_DETAIL_TEXT, NO_OUTPUT_TEXT, REASONING_PREFIX
from .events import SYSTEM_REMINDER_RE, normalize_user_text
from .model import (
    AssistantText,
    Event,
    JsonObj,
    Reasoning,
    SourceError,
    ToolCall,
    UserText,
    as_int,
    as_list,
    as_obj,
    as_str,
)


@dataclass(frozen=True)
class ClaudeSession:
    """One Claude conversation as the catalog shows it."""

    key: str
    cli_id: str
    log_path: Path
    meta_path: Path | None
    title: str
    cwd: str | None
    created_ms: int
    updated_ms: int
    record_count: int

    @property
    def fingerprint(self) -> str:
        """Cheap change detector for the log (size + mtime)."""
        try:
            stat = self.log_path.stat()
        except OSError:
            return ""
        return f"{stat.st_size}:{int(stat.st_mtime)}"


def parse_iso_ms(raw: str) -> int:
    """ISO-8601 -> epoch ms (0 if unparseable)."""
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _scan_log(log: Path) -> tuple[str, int, int, int, str | None]:
    """(title, first_ms, last_ms, conversation records, cwd) from one pass over a log."""
    title, first, last, count, cwd = "", 0, 0, 0, None
    first_user = ""
    with log.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = as_obj(json.loads(line))
            except json.JSONDecodeError:
                continue
            kind = as_str(entry.get("type"))
            if kind == "custom-title" and as_str(entry.get("customTitle")):
                title = as_str(entry.get("customTitle"))
            elif kind in ("user", "assistant") and not entry.get("isSidechain"):
                count += 1
                stamp = parse_iso_ms(as_str(entry.get("timestamp")))
                first = first or stamp
                last = max(last, stamp)
                cwd = cwd or as_str(entry.get("cwd")) or None
                if kind == "user" and not first_user:
                    first_user = _first_text(entry)
    return title or first_user[:80], first, last, count, cwd


def _first_text(entry: JsonObj) -> str:
    content = as_obj(entry.get("message")).get("content")
    if isinstance(content, str):
        return " ".join(SYSTEM_REMINDER_RE.sub("", content).split())
    for block in as_list(content):
        item = as_obj(block)
        if as_str(item.get("type")) == "text":
            text = " ".join(SYSTEM_REMINDER_RE.sub("", as_str(item.get("text"))).split())
            if text:
                return text
    return ""


def list_claude_sessions(paths: AppPaths) -> list[ClaudeSession]:
    """Every Claude session found: desktop-app sessions (via their sidebar record) plus bare Claude Code logs."""
    projects = paths.claude_dir / "projects"
    sessions: dict[str, ClaudeSession] = {}
    claimed: set[str] = set()
    for meta_path in sorted(paths.desktop_dir.glob("*/*/local_*.json")):
        try:
            meta = as_obj(json.loads(meta_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        cli_id = as_str(meta.get("cliSessionId"))
        logs = list(projects.glob(f"*/{cli_id}.jsonl")) if cli_id else []
        if not logs:
            continue
        claimed.add(cli_id)
        scanned = _scan_log(logs[0])
        key = as_str(meta.get("sessionId")) or cli_id
        sessions[key] = ClaudeSession(
            key=key, cli_id=cli_id, log_path=logs[0], meta_path=meta_path,
            title=as_str(meta.get("title")) or scanned[0] or key,
            cwd=as_str(meta.get("cwd")) or scanned[4], created_ms=as_int(meta.get("createdAt")) or scanned[1],
            updated_ms=as_int(meta.get("lastActivityAt")) or scanned[2], record_count=scanned[3],
        )  # fmt: skip
    for log in sorted(projects.glob("*/*.jsonl")):
        if log.stem in claimed:
            continue
        title, first, last, count, cwd = _scan_log(log)
        if count:
            sessions[log.stem] = ClaudeSession(log.stem, log.stem, log, None, title or log.stem, cwd, first, last, count)
    return sorted(sessions.values(), key=lambda s: s.updated_ms, reverse=True)


def _tool_output(content: object) -> str:
    """tool_result content (string or list of blocks) as plain text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in as_list(content):
        item = as_obj(block)
        if as_str(item.get("type")) == "text":
            parts.append(as_str(item.get("text")))
        elif as_str(item.get("type")) == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _collect_results(log: Path) -> dict[str, tuple[str, bool]]:
    """tool_use_id -> (output, is_error) for every tool_result in the log."""
    results: dict[str, tuple[str, bool]] = {}
    with log.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"tool_result"' not in line:
                continue
            try:
                entry = as_obj(json.loads(line))
            except json.JSONDecodeError:
                continue
            for block in as_list(as_obj(entry.get("message")).get("content")):
                item = as_obj(block)
                if as_str(item.get("type")) == "tool_result":
                    results[as_str(item.get("tool_use_id"))] = (_tool_output(item.get("content")), item.get("is_error") is True)
    return results


def active_context_tokens(log: Path) -> int:
    """Rough token size of the chain Claude would send to the model: entries after the last compaction boundary."""
    tokens = 0
    with log.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"compact_boundary"' in line and '"system"' in line:
                tokens = 0
                continue
            if '"user"' not in line and '"assistant"' not in line:
                continue  # cheap prefilter before parsing JSON
            try:
                entry = as_obj(json.loads(line))
            except json.JSONDecodeError:
                continue
            if as_str(entry.get("type")) in ("user", "assistant") and not entry.get("isSidechain") and not entry.get("isMeta"):
                tokens += len(json.dumps(as_obj(entry.get("message")).get("content"), ensure_ascii=False)) // 4
    return tokens


def _skip_entry(entry: JsonObj) -> bool:
    return (
        as_str(entry.get("type")) not in ("user", "assistant")
        or bool(entry.get("isSidechain"))
        or bool(entry.get("isMeta"))
        or bool(entry.get("isCompactSummary"))
        or bool(entry.get("isApiErrorMessage"))
        or bool(entry.get("isVisibleInTranscriptOnly"))
        or bool(entry.get("chatbridgeRecap"))  # copies re-emitted after a compaction (the originals are earlier in the log)
    )


def iter_claude_events(session: ClaudeSession) -> Iterator[Event]:
    """Yield the conversation of a session as events (human text, assistant text, reasoning, tool calls with outputs)."""
    log = session.log_path
    if not log.is_file():
        raise SourceError(f"Claude log missing: {log}")
    results = _collect_results(log)
    seq = 0
    with log.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = as_obj(json.loads(line))
            except json.JSONDecodeError:
                continue
            if _skip_entry(entry):
                continue
            ts = parse_iso_ms(as_str(entry.get("timestamp")))
            content = as_obj(entry.get("message")).get("content")
            if as_str(entry.get("type")) == "user":
                yield from _user_events(content, ts)
            else:
                for event in _assistant_events(content, ts, results, seq):
                    seq += isinstance(event, ToolCall)
                    yield event


def _user_events(content: object, ts: int) -> Iterator[Event]:
    if isinstance(content, str):
        blocks: list[JsonObj] = [{"type": "text", "text": content}]
    else:
        blocks = [as_obj(b) for b in as_list(content)]
    images = sum(1 for b in blocks if as_str(b.get("type")) == "image")
    for block in blocks:
        if as_str(block.get("type")) != "text":
            continue
        text = normalize_user_text(as_str(block.get("text")))
        if not text:
            continue
        yield UserText(text, ts, images)
        images = 0
    if images:
        yield UserText("", ts, images)


def _assistant_events(content: object, ts: int, results: dict[str, tuple[str, bool]], seq: int) -> Iterator[Event]:
    for block in as_list(content):
        item = as_obj(block)
        kind = as_str(item.get("type"))
        if kind == "thinking" and as_str(item.get("thinking")).strip():
            yield Reasoning(as_str(item.get("thinking")), ts)
        elif kind == "text" and as_str(item.get("text")).strip():
            text = as_str(item.get("text"))
            yield Reasoning(text[len(REASONING_PREFIX) :], ts) if text.startswith(REASONING_PREFIX) else AssistantText(text, ts)
        elif kind == "tool_use":
            use_id = as_str(item.get("id"))
            output, is_error = results.get(use_id, ("", False))
            if output in (NO_OUTPUT_TEXT, NO_ERROR_DETAIL_TEXT):
                output = ""
            yield ToolCall(seq, use_id, as_str(item.get("name"), "unknown_tool"), as_obj(item.get("input")), output or None, is_error, ts)
            seq += 1
