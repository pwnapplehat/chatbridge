"""Read-only access to every place Cursor stores chats.

Sources handled:
  * a Cursor profile's globalStorage/state.vscdb  (full fidelity: messages, reasoning, tool calls + results)
  * ~/.cursor/projects/**/agent-transcripts/**.jsonl  (text + tool calls only, no tool results)

The databases are opened read-only; nothing in Cursor's folders is ever modified.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from urllib.parse import unquote, urlparse

from .events import MCP_CLAUDE_PREFIX
from .model import (
    AssistantText,
    ChatRef,
    EmptyRecord,
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

COMPOSER_PREFIX = "composerData:"
BUBBLE_PREFIX = "bubbleId:"
TIMESTAMP_RE = re.compile(r"<timestamp>(.*?)</timestamp>", re.S)
USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
TZ_RE = re.compile(r"\(UTC([+-])(\d{1,2})(?::(\d{2}))?\)")


def key_range(prefix: str) -> tuple[str, str]:
    """Index-friendly half-open key range covering every key that starts with prefix."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def parse_iso_ms(raw: str) -> int:
    """Parse Cursor's ISO-8601 'createdAt' into epoch milliseconds (0 when unparseable)."""
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def parse_json_text(raw: object) -> JsonObj | None:
    """Parse a JSON-object string (or pass through a dict); None if it is neither."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI into a filesystem path."""
    return unquote(urlparse(uri).path) if uri.startswith("file://") else uri


@dataclass(frozen=True)
class CursorProfile:
    """One Cursor user-data directory (the folder that contains globalStorage/ and workspaceStorage/)."""

    user_dir: Path
    label: str
    writable: bool = False

    @property
    def db_path(self) -> Path:
        return self.user_dir / "globalStorage" / "state.vscdb"

    def workspace_folders(self) -> dict[str, str]:
        """Map workspaceStorage hash -> project folder path."""
        folders: dict[str, str] = {}
        for meta in (self.user_dir / "workspaceStorage").glob("*/workspace.json"):
            try:
                folder = as_str(as_obj(json.loads(meta.read_text(encoding="utf-8"))).get("folder"))
            except (OSError, json.JSONDecodeError):
                continue
            if folder:
                folders[meta.parent.name] = uri_to_path(folder)
        return folders


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a Cursor SQLite file strictly read-only (immutable when no WAL is present)."""
    if not db_path.is_file():
        raise SourceError(f"database not found: {db_path}")
    wal = db_path.with_name(db_path.name + "-wal")
    flags = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    conn = sqlite3.connect(f"file:{db_path.as_posix().replace(' ', '%20')}?{flags}", uri=True)
    conn.execute("PRAGMA query_only=1")
    return conn


def _load(raw: object) -> JsonObj:
    """Decode a stored value (text or bytes) into a JSON object."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else as_str(raw)
    try:
        return as_obj(json.loads(text))
    except json.JSONDecodeError as exc:
        raise SourceError(f"corrupt JSON record: {exc}") from exc


def list_db_chats(profile: CursorProfile) -> list[ChatRef]:
    """Enumerate every chat in a profile's database, including empty drafts."""
    conn = open_readonly(profile.db_path)
    try:
        folders = profile.workspace_folders()
        heads = {cid: (ws, bool(sub)) for cid, ws, sub in conn.execute("SELECT composerId, workspaceId, isSubagent FROM composerHeaders")}
        low, high = key_range(COMPOSER_PREFIX)
        chats: list[ChatRef] = []
        for key, raw in conn.execute("SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?", (low, high)):
            chat_id = key[len(COMPOSER_PREFIX) :]
            data = _load(raw)
            ws_hash, is_sub = heads.get(chat_id, ("", False))
            cwd = as_str(as_obj(as_obj(data.get("workspaceIdentifier")).get("uri")).get("fsPath")) or folders.get(ws_hash)
            blow, bhigh = key_range(f"{BUBBLE_PREFIX}{chat_id}:")
            rows = conn.execute("SELECT count(*) FROM cursorDiskKV WHERE key >= ? AND key < ?", (blow, bhigh)).fetchone()[0]
            headers = as_list(data.get("fullConversationHeadersOnly"))
            chats.append(
                ChatRef(
                    chat_id=chat_id,
                    name=as_str(data.get("name")),
                    created_ms=as_int(data.get("createdAt")),
                    cwd=cwd,
                    is_subagent=is_sub,
                    kind="db",
                    source_path=profile.db_path,
                    source_label=profile.label,
                    record_count=int(rows),
                    updated_ms=as_int(data.get("lastUpdatedAt")),
                    expected_visible=len(headers),
                    preview=_first_user_preview(headers),
                )
            )
        return chats
    finally:
        conn.close()


def _first_user_preview(headers: list[object]) -> str:
    """Text preview of the first human message, taken from the chat's header list (cheap, no bubble reads)."""
    for header in headers:
        item = as_obj(header)
        if as_int(item.get("type")) == 1:
            return " ".join(as_str(as_obj(item.get("grouping")).get("textPreview")).split())[:200]
    return ""


def _normalize_bubble(bubble: JsonObj, seq_start: int) -> list[Event]:
    """Turn one stored bubble into zero or more events (an EmptyRecord when it has no content)."""
    created = parse_iso_ms(as_str(bubble.get("createdAt")))
    events: list[Event] = []
    if as_int(bubble.get("type")) == 1:
        text = as_str(bubble.get("text"))
        images = len(as_list(bubble.get("images")))
        return [UserText(text, created, images)] if (text.strip() or images) else [EmptyRecord()]
    thinking = bubble.get("thinking")
    thought = as_str(as_obj(parse_json_text(thinking)).get("text")) if parse_json_text(thinking) else as_str(thinking)
    if thought.strip():
        events.append(Reasoning(thought, created))
    text = as_str(bubble.get("text"))
    if text.strip():
        events.append(AssistantText(text, created))
    tool = as_obj(bubble.get("toolFormerData"))
    if tool:
        events.append(_tool_event(tool, created, seq_start))
    return events or [EmptyRecord()]


def _unwrap_claude_tool(tool: JsonObj) -> tuple[str, JsonObj, object] | None:
    """Tool calls written by ChatBridge from Claude (mcp-claude-<name>) -> (name, input, plain output)."""
    name = as_str(tool.get("name"))
    if not name.startswith(MCP_CLAUDE_PREFIX):
        return None
    wrapped = as_list(as_obj(parse_json_text(tool.get("params"))).get("tools"))
    tool_input = parse_json_text(as_str(as_obj(wrapped[0]).get("parameters"))) if wrapped else None
    output: object = tool.get("result")
    outer = parse_json_text(output)
    inner = parse_json_text(as_obj(outer).get("result")) if outer else None
    if inner is not None:
        parts = [as_str(as_obj(b).get("text")) for b in as_list(inner.get("content"))]
        output = "\n".join(parts) if parts else None
    return name[len(MCP_CLAUDE_PREFIX) :], tool_input or {}, output


def _tool_event(tool: JsonObj, created: int, seq: int) -> ToolCall:
    """Build a ToolCall from Cursor's toolFormerData block."""
    claude = _unwrap_claude_tool(tool)
    name = as_str(tool.get("name"), "unknown_tool")
    params = parse_json_text(tool.get("params"))
    raw_args = parse_json_text(tool.get("rawArgs"))
    chosen = params if params else raw_args
    tool_input: JsonObj = chosen if chosen else {"raw": as_str(tool.get("params")) or as_str(tool.get("rawArgs"))}
    result = tool.get("result")
    if claude is not None:
        name, tool_input, result = claude[0], claude[1], claude[2] if claude[2] != "" else None
    error = tool.get("error")
    is_error = as_str(tool.get("status")) == "error" or bool(error)
    output: str | None
    if isinstance(result, str):
        output = result
    elif result is not None:
        output = json.dumps(result, ensure_ascii=False)
    elif error:
        output = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    else:
        output = None
    return ToolCall(seq, as_str(tool.get("toolCallId")), name, tool_input, output, is_error, created)


def iter_db_events(chat: ChatRef) -> Iterator[Event]:
    """Yield every event of a DB chat in chronological order, using *all* stored bubbles.

    The header list in composerData omits most tool/reasoning bubbles of large chats, so
    ordering is derived from every bubble row (createdAt), with header position as tie-break.
    """
    conn = open_readonly(chat.source_path)
    try:
        data = _load(conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_PREFIX + chat.chat_id,)).fetchone()[0])
        head_pos = {as_str(as_obj(h).get("bubbleId")): i for i, h in enumerate(as_list(data.get("fullConversationHeadersOnly")))}
        low, high = key_range(f"{BUBBLE_PREFIX}{chat.chat_id}:")
        order: list[tuple[int, int, int, str]] = []
        for row_no, (key, raw) in enumerate(conn.execute("SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?", (low, high))):
            bubble = _load(raw)
            bid = key.rsplit(":", 1)[1]
            order.append((parse_iso_ms(as_str(bubble.get("createdAt"))), head_pos.get(bid, 1 << 30), row_no, key))
        order.sort()
        seq = 0
        for _, _, _, key in order:
            row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
            for event in _normalize_bubble(_load(row[0]), seq):
                if isinstance(event, ToolCall):
                    seq += 1
                yield event
    finally:
        conn.close()


def _parse_cursor_timestamp(raw: str) -> int:
    """Parse 'Monday, Sep 14, 2026, 12:04 PM (UTC+5:30)' into epoch ms (0 when unparseable)."""
    match = TZ_RE.search(raw)
    if not match:
        return 0
    sign = 1 if match.group(1) == "+" else -1
    offset = sign * (int(match.group(2)) * 60 + int(match.group(3) or 0))
    try:
        naive = datetime.strptime(TZ_RE.sub("", raw).strip(), "%A, %b %d, %Y, %I:%M %p")
    except ValueError:
        return 0
    return int((naive.replace(tzinfo=UTC).timestamp() - offset * 60) * 1000)


def list_transcript_chats(projects_dir: Path, folder_by_slug: dict[str, str]) -> list[ChatRef]:
    """Enumerate agent-transcript JSONL files (all depths, including subagent runs)."""
    chats: list[ChatRef] = []
    for path in sorted(projects_dir.glob("*/agent-transcripts/**/*.jsonl")):
        slug = path.relative_to(projects_dir).parts[0]
        count = sum(1 for line in path.open(encoding="utf-8") if '"role"' in line)
        mtime = int(path.stat().st_mtime * 1000)
        chats.append(
            ChatRef(
                chat_id=path.stem,
                name="",
                created_ms=mtime,
                cwd=folder_by_slug.get(slug.strip("-")),
                preview=_transcript_preview(path),
                is_subagent="subagents" in path.parts or path.stem.startswith("agent-"),
                kind="transcript",
                source_path=path,
                source_label=f"transcript:{slug}",
                record_count=count,
                updated_ms=mtime,
            )
        )
    return chats


def _transcript_preview(path: Path) -> str:
    """First human message of a transcript (reads only until it is found)."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = as_obj(json.loads(line)) if line.strip() else {}
            if as_str(record.get("role")) == "user":
                for block in as_list(as_obj(record.get("message")).get("content")):
                    text = as_str(as_obj(block).get("text"))
                    query = USER_QUERY_RE.search(text)
                    return " ".join((query.group(1) if query else TIMESTAMP_RE.sub("", text)).split())[:200]
    return ""


def iter_transcript_events(chat: ChatRef) -> Iterator[Event]:
    """Yield events from an agent-transcript file. Tool calls have no stored output."""
    seq = 0
    clock = chat.created_ms
    with chat.source_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = as_obj(json.loads(line))
            role = as_str(record.get("role"))
            if role not in ("user", "assistant"):
                continue
            for block in as_list(as_obj(record.get("message")).get("content")):
                item = as_obj(block)
                kind = as_str(item.get("type"))
                if kind == "text" and role == "user":
                    text = as_str(item.get("text"))
                    stamp = TIMESTAMP_RE.search(text)
                    clock = _parse_cursor_timestamp(stamp.group(1)) if stamp else clock
                    yield UserText(text, clock or chat.created_ms)
                elif kind == "text":
                    yield AssistantText(as_str(item.get("text")), clock or chat.created_ms)
                elif kind == "tool_use":
                    yield ToolCall(
                        seq,
                        f"{chat.chat_id}-{seq}",
                        as_str(item.get("name"), "unknown_tool"),
                        as_obj(item.get("input")),
                        None,
                        False,
                        clock or chat.created_ms,
                    )
                    seq += 1
                else:
                    yield EmptyRecord()
            clock += 1000


def iter_events(chat: ChatRef) -> Iterator[Event]:
    """Dispatch to the right reader for the chat's source kind."""
    return iter_db_events(chat) if chat.kind == "db" else iter_transcript_events(chat)


def folder_slug_index(folders: list[str]) -> dict[str, str]:
    """Index project folders by Cursor's transcript-directory slug (non-alphanumerics -> '-')."""
    return {re.sub(r"[^A-Za-z0-9]", "-", f).strip("-"): f for f in folders}


def preview_events(chat: ChatRef, limit: int) -> list[Event]:
    """First `limit` events of a chat for on-screen preview, without reading the whole chat.

    DB chats use the header list (the user/assistant messages Cursor itself displays), so this is
    fast even for chats with hundreds of thousands of records. It is only a preview: the import
    reads every record.
    """
    if chat.kind == "transcript":
        return list(islice(iter_transcript_events(chat), limit))
    conn = open_readonly(chat.source_path)
    try:
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_PREFIX + chat.chat_id,)).fetchone()
        if row is None:
            raise SourceError(f"chat {chat.chat_id} no longer exists in {chat.source_path}")
        events: list[Event] = []
        for header in as_list(_load(row[0]).get("fullConversationHeadersOnly")):
            bubble_id = as_str(as_obj(header).get("bubbleId"))
            found = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"{BUBBLE_PREFIX}{chat.chat_id}:{bubble_id}",)).fetchone()
            if found is None:
                continue
            events.extend(e for e in _normalize_bubble(_load(found[0]), 0) if not isinstance(e, EmptyRecord))
            if len(events) >= limit:
                break
        return events[:limit]
    finally:
        conn.close()
