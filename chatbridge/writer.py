"""Write converted chats into Claude's session store and verify what was written.

Two files make a session visible in the desktop app:
  ~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl                      (conversation log)
  ~/.config/Claude/claude-code-sessions/<org>/<account>/local_<uuid>.json (sidebar metadata)

Session ids are derived from the Cursor chat id (uuid5), so re-running an import is
idempotent: an existing log is never overwritten.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from .compaction import CompactionPlan, EventScan, plan_compaction
from .converter import (
    ATTACHMENT_PREFIX,
    NO_ERROR_DETAIL_TEXT,
    NO_OUTPUT_TEXT,
    OPENER_PREFIX,
    REASONING_PREFIX,
    build_compaction_tail,
    build_continuation,
    convert,
    count_event,
    derive_title,
)
from .model import (
    ChatRef,
    ConversionReport,
    Counts,
    Event,
    ImporterError,
    JsonObj,
    UserText,
    WriteError,
    as_list,
    as_obj,
    as_str,
)
from .validate import validate

NAMESPACE = uuid.UUID("6f1b6a0e-2f0c-4a54-9a8e-3c2f4f6d8a11")
DEFAULT_MODEL = "claude-sonnet-5-5"
PLACEHOLDERS = {NO_OUTPUT_TEXT, NO_ERROR_DETAIL_TEXT}


def session_uuid(chat_id: str) -> str:
    """Stable Claude session id for a Cursor chat."""
    return str(uuid.uuid5(NAMESPACE, f"cursor-chat:{chat_id}"))


def local_session_name(chat_id: str) -> str:
    """Stable file stem of the desktop sidebar metadata for a Cursor chat."""
    return "local_" + str(uuid.uuid5(NAMESPACE, f"local:{chat_id}"))


def cwd_slug(cwd: str) -> str:
    """Claude Code's project-directory slug (every non-alphanumeric character becomes '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def resolve_cwd(candidate: str | None, aliases: dict[str, str] | None = None) -> str:
    """Pick the folder a restored session belongs to.

    Order: the chat's own folder if it exists (also trying the /run/media <-> /mnt alias),
    then an existing folder with the same final name (maps old Windows paths such as
    'd:\\Work\\ExampleApp' to today's Linux folder), else $HOME.
    """
    if candidate:
        options = [candidate]
        if candidate.startswith("/run/media/"):
            options.append("/mnt/" + candidate.split("/", 4)[4])
        elif candidate.startswith("/mnt/"):
            options.append("/run/media/" + os.environ.get("USER", "") + "/" + candidate[len("/mnt/") :])
        for option in options:
            if Path(option).is_dir():
                return option
        base = re.split(r"[\\/]+", candidate.strip("/\\"))[-1].lower()
        if aliases and base in aliases:
            return aliases[base]
    return str(Path.home())


def folder_aliases(folders: Iterable[str]) -> dict[str, str]:
    """Index existing project folders by lower-cased final name (first one wins)."""
    index: dict[str, str] = {}
    for folder in folders:
        if Path(folder).is_dir():
            index.setdefault(Path(folder).name.lower(), folder)
    return index


def find_sessions_dir_or_none(desktop_dir: Path) -> Path | None:
    """The <org>/<account> directory of the Claude desktop app, or None when the app has never created one."""
    found = sorted(desktop_dir.glob("*/*/local_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0].parent if found else None


def find_sessions_dir(desktop_dir: Path) -> Path:
    """Locate the <org>/<account> directory that already holds desktop session metadata."""
    found = sorted(desktop_dir.glob("*/*/local_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not found:
        raise WriteError(f"no existing local_*.json under {desktop_dir}; open a Claude Code session once, or pass --sessions-dir")
    return found[0].parent


def count_written(log_path: Path) -> Counts:
    """Re-read a written log and tally content the same way the source tally is built."""
    user = assistant = reasoning = calls = results = chars = 0
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            entry = as_obj(json.loads(line))
            kind = as_str(entry.get("type"))
            if kind not in ("user", "assistant") or entry.get("chatbridgeRecap") or entry.get("isCompactSummary"):
                continue  # compaction copies/summary are context for the model, not conversation content
            blocks = [as_obj(b) for b in as_list(as_obj(entry.get("message")).get("content"))]
            is_opener = kind == "user" and any(as_str(b.get("text")).startswith(OPENER_PREFIX) for b in blocks)
            if kind == "user" and not is_opener and any(b.get("type") == "text" for b in blocks):
                user += 1  # one human message per entry, including image-only messages (note block only)
            for item in blocks:
                btype = as_str(item.get("type"))
                text = as_str(item.get("text"))
                if btype == "tool_use":
                    calls += 1
                elif btype == "tool_result":
                    results += 1
                    content = as_str(item.get("content"))
                    chars += 0 if content in PLACEHOLDERS else len(content)
                elif btype == "text" and kind == "user":
                    chars += 0 if text.startswith((ATTACHMENT_PREFIX, OPENER_PREFIX)) else len(text)
                elif btype == "text" and text.startswith(REASONING_PREFIX):
                    reasoning += 1
                    chars += len(text) - len(REASONING_PREFIX)
                elif btype == "text":
                    assistant += 1
                    chars += len(text)
    return Counts(user, assistant, reasoning, calls, results, chars)


def _metadata(chat: ChatRef, session_id: str, cwd: str, title: str, counts: Counts, last_assistant: str) -> JsonObj:
    now_ms = int(time.time() * 1000)
    return {
        "sessionId": local_session_name(chat.chat_id),
        "cliSessionId": session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "lastFocusedAt": now_ms,
        "createdAt": chat.created_ms or now_ms,
        "lastActivityAt": now_ms,
        "model": DEFAULT_MODEL,
        "effort": "medium",
        "isArchived": False,
        "title": title,
        "titleSource": "user",
        "permissionMode": "default",
        "completedTurns": counts.user,
        "titleTurn": 0,
        "lastAssistantUuid": last_assistant,
        "envScopeId": "builtin_local",
    }


def _write_log(entries: Iterable[JsonObj], final: Path) -> tuple[int, str]:
    """Stream entries to <final>.partial, then atomically rename. Returns (bytes, last assistant uuid)."""
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name + ".partial")
    last_assistant = ""
    try:
        with partial.open("w", encoding="utf-8") as handle:
            for entry in entries:
                if entry.get("type") == "assistant":
                    last_assistant = as_str(entry.get("uuid"))
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return final.stat().st_size, last_assistant


def last_assistant_uuid(log_path: Path) -> str:
    """uuid of the final assistant entry in an existing log."""
    last = ""
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            entry = as_obj(json.loads(line))
            if entry.get("type") == "assistant":
                last = as_str(entry.get("uuid"))
    return last


def tally_events(events: Iterable[Event]) -> tuple[Counts, str]:
    """Read a whole event stream once, returning content counts and the first user message."""
    counts = Counts()
    first_user = ""
    for event in events:
        counts = count_event(counts, event)
        if isinstance(event, UserText) and not first_user:
            first_user = event.text
    return counts, first_user


def scan_events(events: Iterable[Event]) -> tuple[Counts, str, EventScan]:
    """One pass over a conversation: content counts, the first user message and the data the compaction planner needs."""
    counts = Counts()
    first_user = ""
    scan = EventScan()
    for event in events:
        counts = count_event(counts, event)
        scan.add(event)
        if isinstance(event, UserText) and not first_user:
            first_user = event.text
    return counts, first_user, scan


def import_chat(
    chat: ChatRef,
    open_events: Callable[[], Iterable[Event]],
    claude_dir: Path,
    sessions_dir: Path | None,
    apply: bool,
    aliases: dict[str, str] | None = None,
    cwd_override: str | None = None,
) -> ConversionReport:
    """Convert one chat in two passes (tally, then write). Without apply nothing is written.

    open_events must return a fresh event stream on every call.
    """
    session_id = session_uuid(chat.chat_id)
    cwd = cwd_override or resolve_cwd(chat.cwd, aliases)
    log_path = claude_dir / "projects" / cwd_slug(cwd) / f"{session_id}.jsonl"
    report = ConversionReport(chat.chat_id, derive_title(chat.name, ""), chat.source_label, "dry-run", cwd, str(log_path))
    quarantined = log_path.with_name(log_path.name + ".unverified")
    if log_path.exists():
        report.status, report.detail = "exists", "already imported; left untouched"
        return report
    source, first_user, scan = scan_events(open_events())
    report.source_counts = source
    report.title = derive_title(chat.name, first_user)
    if source.content_tuple() == Counts().content_tuple():
        report.status, report.detail = "empty", "no user-visible content in the source"
        return report
    plan = plan_compaction(scan, report.title, f"Cursor ({chat.source_label})")
    if plan is not None:
        report.detail = (
            f"long conversation (about {plan.pre_tokens:,} tokens): the full history is kept in the log, and Claude's active context is "
            f"compacted to about {plan.post_tokens:,} tokens (the first {plan.omitted:,} messages are summarized, the latest {plan.recent:,} follow)"
        )
    if not apply:
        return report
    if quarantined.exists():
        os.replace(quarantined, log_path)  # re-verify a previously quarantined log instead of rewriting it
        report.detail = "recovered previously quarantined log"
        size, last_assistant = log_path.stat().st_size, last_assistant_uuid(log_path)
    else:
        size, last_assistant = _write_log(
            convert(open_events(), session_id, chat.chat_id, cwd, report.title, plan, str(log_path)), log_path
        )
    report.log_bytes = size
    report.written_counts = count_written(log_path)
    problems = validate(log_path)
    if not report.verified or problems:
        report.status = "failed"
        report.detail = (
            f"structural problem: {problems[0]}"
            if problems
            else f"verification mismatch: source={source.content_tuple()} written={report.written_counts.content_tuple()}"
        )
        log_path.rename(log_path.with_name(log_path.name + ".unverified"))
        return report
    if sessions_dir is not None:
        meta = _metadata(chat, session_id, cwd, report.title, source, last_assistant)
        (sessions_dir / f"{as_str(meta['sessionId'])}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        report.detail = "no Claude desktop sessions folder found: wrote the session log only (open it with `claude --resume`)"
    report.status = "written"
    return report


class ClaudeBusyError(ImporterError):
    """The Claude log changed very recently (a turn may be in progress); try again in a moment."""


def read_log_tail(log_path: Path) -> tuple[str | None, int, str, str]:
    """(last conversation uuid, its timestamp ms, session cwd, session id) of an existing log."""
    last_uuid: str | None = None
    last_ts = 0
    cwd = ""
    session_id = log_path.stem
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = as_obj(json.loads(line))
            except json.JSONDecodeError:
                continue
            if as_str(entry.get("type")) in ("user", "assistant") and not entry.get("isSidechain"):
                last_uuid = as_str(entry.get("uuid")) or last_uuid
                stamp = as_str(entry.get("timestamp"))
                last_ts = max(last_ts, int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)) if stamp else last_ts
                cwd = as_str(entry.get("cwd")) or cwd
                session_id = as_str(entry.get("sessionId")) or session_id
    return last_uuid, last_ts, cwd, session_id


def append_events_to_claude(log_path: Path, meta_path: Path | None, chat_id: str, events: list[Event], settle_seconds: float = 3.0) -> int:
    """Append events to an existing Claude log (history is never rewritten). Returns the number of entries written.

    Refuses (ClaudeBusyError) when the log was modified within `settle_seconds`, because the Claude app may be
    mid-turn. The new lines are written with a single write + fsync, so a crash leaves the chain valid up to the last
    complete line.
    """
    if time.time() - log_path.stat().st_mtime < settle_seconds:
        raise ClaudeBusyError(f"{log_path.name} was modified moments ago; the Claude app may be mid-turn")
    parent, last_ts, cwd, session_id = read_log_tail(log_path)
    entries, last_uuid, last_user = build_continuation(events, session_id, chat_id, cwd or str(Path.home()), parent, last_ts)
    if not entries:
        return 0
    _commit_entries(log_path, meta_path, session_id, entries, last_uuid, last_user)
    return len(entries)


def _commit_entries(
    log_path: Path, meta_path: Path | None, session_id: str, entries: list[JsonObj], last_uuid: str | None, last_user: str
) -> None:
    """Append entries (+ a last-prompt trailer) with one write + fsync and refresh the desktop record."""
    trailer = {"type": "last-prompt", "lastPrompt": last_user[:200], "leafUuid": last_uuid, "sessionId": session_id}
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [*entries, trailer])
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if meta_path is not None and meta_path.is_file():
        meta = as_obj(json.loads(meta_path.read_text(encoding="utf-8")))
        last_assistant = next((as_str(e.get("uuid")) for e in reversed(entries) if e.get("type") == "assistant"), "")
        meta["lastActivityAt"] = int(time.time() * 1000)
        if last_assistant:
            meta["lastAssistantUuid"] = last_assistant
        meta["completedTurns"] = int(meta.get("completedTurns") or 0) + sum(1 for e in entries if e.get("type") == "assistant")  # type: ignore[call-overload]
        partial = meta_path.with_name(meta_path.name + ".partial")
        partial.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(partial, meta_path)


def compact_claude_log(
    log_path: Path, meta_path: Path | None, chat_id: str, events: list[Event], plan: CompactionPlan, settle_seconds: float = 3.0
) -> int:
    """Append a compaction (boundary + summary + flagged copies of the recent turns) to an oversized log.

    History is never rewritten: the new entries start a new chain after the existing log, so Claude's context becomes
    the summary plus the recent turns while everything earlier stays in the file. Returns the number of entries written.
    """
    if time.time() - log_path.stat().st_mtime < settle_seconds:
        raise ClaudeBusyError(f"{log_path.name} was modified moments ago; the Claude app may be mid-turn")
    parent, last_ts, cwd, session_id = read_log_tail(log_path)
    entries, last_uuid, last_user = build_compaction_tail(
        events[plan.recent_start :], session_id, chat_id, cwd or str(Path.home()), parent, last_ts, plan, str(log_path)
    )
    _commit_entries(log_path, meta_path, session_id, entries, last_uuid, last_user)
    return len(entries)
