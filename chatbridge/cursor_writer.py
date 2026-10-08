"""Write conversation events into a Cursor profile's database, faithfully and reversibly.

Records mirror what Cursor itself stores (shapes taken from a real chat, see scripts/gen_cursor_templates.py):
a ``composerData:<chat>`` row, one ``bubbleId:<chat>:<bubble>`` row per message (user text, assistant text,
reasoning, tool call) listed in ``fullConversationHeadersOnly``, and a ``composerHeaders`` row that places the chat
in a workspace. Claude-origin tool calls are stored as MCP-style tool calls (server "claude"), which Cursor renders
generically.

Safety rules:
  * never write while Cursor is running on that profile (it would overwrite or corrupt state);
  * one SQLite transaction per chat; a journal of every touched row is written first so the change can be undone;
  * existing rows are only modified to append to the header list / bump timestamps, never deleted.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from .cursor_source import CursorProfile, uri_to_path
from .events import MCP_CLAUDE_PREFIX
from .model import (
    AssistantText,
    EmptyRecord,
    Event,
    ImporterError,
    JsonObj,
    Reasoning,
    ToolCall,
    UserText,
    as_list,
    as_obj,
    as_str,
)
from .writer import NAMESPACE

COMPOSER_KEY = "composerData:"
BUBBLE_KEY = "bubbleId:"
DEFAULT_MODEL = "default"
CONTEXT_LIMIT = 256000


class CursorBusyError(ImporterError):
    """Cursor is running on the target profile; writing now could corrupt or lose data."""


class CursorWriteError(ImporterError):
    """The Cursor database could not be written."""


@dataclass(frozen=True)
class WriteResult:
    """Outcome of writing events into one Cursor chat."""

    composer_id: str
    created: bool
    bubbles_added: int
    journal_path: Path | None


def cursor_chat_id_for_claude(claude_key: str) -> str:
    """Deterministic Cursor chat id for a Claude session (so a re-import finds the same chat)."""
    return str(uuid.uuid5(NAMESPACE, f"claude-session:{claude_key}"))


# --------------------------------------------------------------------------- running detection
def _main_process_profiles() -> list[Path]:
    """User-data dirs of all running Cursor main processes (default profile when no --user-data-dir)."""
    found: list[Path] = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        text = [a.decode("utf-8", "replace") for a in args if a]
        if not text or "--type=" in " ".join(text) or ("/cursor/cursor" not in text[0] and Path(text[0]).name != "cursor"):
            continue
        explicit = next((a.split("=", 1)[1] for a in text if a.startswith("--user-data-dir=")), None)
        found.append(Path(explicit) if explicit else Path.home() / ".config" / "Cursor")
    return found


def cursor_running(profile: CursorProfile) -> bool:
    """True when a Cursor main process uses this profile's data directory."""
    root = profile.user_dir.parent.resolve()
    return any(p.resolve() == root for p in _main_process_profiles())


# --------------------------------------------------------------------------- templates
def _templates() -> dict[str, JsonObj]:
    raw = resources.files("chatbridge").joinpath("data/cursor_templates.json").read_text(encoding="utf-8")
    return {k: as_obj(v) for k, v in as_obj(json.loads(raw)).items()}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def _lexical(text: str) -> str:
    """Cursor's rich-text (Lexical) JSON for a plain message: one paragraph per line."""
    paragraphs: list[JsonObj] = []
    for line in text.split("\n"):
        children: list[JsonObj] = (
            [{"detail": 0, "format": 0, "mode": "normal", "style": "", "text": line, "type": "text", "version": 1}] if line else []
        )
        paragraphs.append({"children": children, "direction": "ltr", "format": "", "indent": 0, "type": "paragraph", "version": 1})
    root: JsonObj = {"children": paragraphs, "direction": "ltr", "format": "", "indent": 0, "type": "root", "version": 1}
    return json.dumps({"root": root}, ensure_ascii=False, separators=(",", ":"))


def _preview(text: str) -> str:
    return " ".join(text.split())[:200]


def _tool_record(call: ToolCall) -> JsonObj:
    """toolFormerData for a Claude-origin tool call, in Cursor's MCP-tool shape."""
    call_id = f"call-{uuid.uuid4()}-{call.seq}"
    name = f"{MCP_CLAUDE_PREFIX}{call.name}"
    params = {"tools": [{"name": call.name, "parameters": json.dumps(call.tool_input, ensure_ascii=False), "serverName": "claude"}]}
    raw_args = {"name": f"user-claude-{call.name}", "args": call.tool_input, "toolCallId": call_id,
                "providerIdentifier": "claude", "toolName": call.name, "serverIdentifier": "user-claude"}  # fmt: skip
    content = [{"type": "text", "text": call.output or ""}]
    record: JsonObj = {
        "tool": 19, "toolIndex": 0, "modelCallId": call_id, "toolCallId": call_id,
        "status": "error" if call.is_error else "completed", "rawArgs": json.dumps(raw_args, ensure_ascii=False),
        "name": name, "params": json.dumps(params, ensure_ascii=False),
        "additionalData": {"status": "error" if call.is_error else "success"},
    }  # fmt: skip
    if call.output is not None:
        record["result"] = json.dumps({"result": json.dumps({"content": content}, ensure_ascii=False)}, ensure_ascii=False)
    return record


def build_bubble(event: Event, bubble_id: str, ts_ms: int, tpl: dict[str, JsonObj]) -> tuple[JsonObj, JsonObj] | None:
    """(stored bubble, header item) for one event; None for events that carry nothing."""
    created = _iso(ts_ms)
    bubble: JsonObj = {
        **tpl["bubble_base"],
        "bubbleId": bubble_id,
        "createdAt": created,
        "unifiedMode": 2,
        "conversationState": "~",
        "requestId": "",
    }
    grouping: JsonObj = {"isRenderable": True, "toolDisplayComputed": True}
    if isinstance(event, UserText):
        if not event.text.strip():
            return None
        bubble |= {
            "type": 1,
            "text": event.text,
            "richText": _lexical(event.text),
            **tpl["user_extra"],
            "modelInfo": {"modelName": DEFAULT_MODEL},
        }
        bubble["requestId"] = str(uuid.uuid4())
        grouping |= {"hasText": True, "textPreview": _preview(event.text)}
    elif isinstance(event, AssistantText):
        bubble |= {"type": 2, "text": event.text}
        grouping |= {"hasText": True, "textPreview": _preview(event.text)}
    elif isinstance(event, Reasoning):
        bubble |= {
            "type": 2,
            "text": "",
            "capabilityType": 30,
            "thinkingStyle": 1,
            "thinking": {"text": event.text},
            "thinkingDurationMs": 0,
        }
        grouping |= {"capabilityType": 30, "hasThinking": True, "thinkingDurationMs": 0}
    elif isinstance(event, ToolCall):
        record = _tool_record(event)
        bubble |= {"type": 2, "text": "", "capabilityType": 15, "toolFormerData": record}
        grouping |= {"capabilityType": 15, "toolFormerTool": 19, "toolFormerStatus": record["status"], "toolCallId": record["toolCallId"]}
    else:
        return None
    return bubble, {"bubbleId": bubble_id, "type": bubble["type"], "grouping": grouping, "createdAt": created}


# --------------------------------------------------------------------------- workspace + composer records
def workspace_hash(folder: str) -> str | None:
    """The workspace id Cursor/VS Code assigns to a folder on Linux: md5(path + inode). None if the folder is gone."""
    try:
        inode = os.stat(folder).st_ino
    except OSError:
        return None
    return hashlib.md5(f"{folder}{inode}".encode()).hexdigest()


def workspace_for_folder(profile: CursorProfile, folder: str | None) -> tuple[str, JsonObj]:
    """(workspace id, workspaceIdentifier) for a folder.

    Order: a workspace this profile already knows for the folder; else the id Cursor would assign when the folder is
    first opened (so the chat appears as soon as the folder is opened); else the 'empty-window' (no folder) workspace.
    """
    if folder:
        wanted = {folder, folder.replace("/run/media/" + os.environ.get("USER", ""), "/mnt", 1)}
        for ws_hash, known in profile.workspace_folders().items():
            if known in wanted or uri_to_path(known) in wanted:
                return ws_hash, _folder_identifier(ws_hash, known)
        computed = workspace_hash(folder)
        if computed is not None:
            return computed, _folder_identifier(computed, folder)
    return "empty-window", {"id": "empty-window"}


def _folder_identifier(ws_hash: str, folder: str) -> JsonObj:
    uri = {"$mid": 1, "fsPath": folder, "external": f"file://{folder}", "path": folder, "scheme": "file"}
    return {"id": ws_hash, "uri": uri}


def _random_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def new_composer(tpl: dict[str, JsonObj], composer_id: str, name: str, created_ms: int, workspace: JsonObj) -> JsonObj:
    """A fresh composerData record shaped like Cursor's own."""
    composer: JsonObj = {
        **tpl["composer"], "composerId": composer_id, "name": name, "richText": _lexical(""), "text": "",
        "fullConversationHeadersOnly": [], "createdAt": created_ms, "lastUpdatedAt": created_ms,
        "conversationCheckpointLastUpdatedAt": created_ms, "contextUsagePercent": 0, "contextTokensUsed": 0,
        "promptTokenBreakdown": {"totalUsedTokens": 0, "maxTokens": CONTEXT_LIMIT, "categories": []},
        "modelConfig": {"modelName": DEFAULT_MODEL, "maxMode": False, "selectedModels": [{"modelId": DEFAULT_MODEL, "parameters": []}]},
        "todos": [], "originalFileStates": {}, "newlyCreatedFiles": [], "trackedGitRepos": [], "subtitle": "",
        "filesChangedCount": 0, "totalLinesAdded": 0, "totalLinesRemoved": 0, "latestChatGenerationUUID": str(uuid.uuid4()),
        "blobEncryptionKey": _random_key(), "speculativeSummarizationEncryptionKey": _random_key(), "workspaceIdentifier": workspace,
    }  # fmt: skip
    return composer


def header_value(composer: JsonObj, workspace: JsonObj) -> JsonObj:
    """The compact 'head' record stored in composerHeaders.value."""
    return {
        "type": "head", "composerId": composer["composerId"], "name": composer["name"], "lastUpdatedAt": composer["lastUpdatedAt"],
        "conversationCheckpointLastUpdatedAt": composer["conversationCheckpointLastUpdatedAt"], "createdAt": composer["createdAt"],
        "unifiedMode": "agent", "forceMode": "edit", "hasUnreadMessages": False, "contextUsagePercent": 0, "totalLinesAdded": 0,
        "totalLinesRemoved": 0, "filesChangedCount": 0, "subtitle": as_str(composer.get("subtitle")), "hasBlockingPendingActions": False,
        "hasPendingPlan": False, "isDraft": False, "isWorktree": False, "worktreeStartedReadOnly": False, "isSpec": False,
        "isProject": False, "isBestOfNSubcomposer": False, "numSubComposers": 0, "referencedPlans": [], "trackedGitRepos": [],
        "workspaceIdentifier": workspace,
    }  # fmt: skip


# --------------------------------------------------------------------------- the write
def _spread_timestamps(events: list[Event], floor_ms: int) -> list[int]:
    """Unique, non-decreasing millisecond timestamps (bubble createdAt must order messages)."""
    stamps: list[int] = []
    last = floor_ms
    for event in events:
        ts = max(getattr(event, "ts_ms", 0) or last, last)
        stamps.append(ts if not stamps or ts > stamps[-1] else stamps[-1] + 1)
        last = stamps[-1]
    return stamps


def upsert_events(
    profile: CursorProfile,
    composer_id: str,
    name: str,
    folder: str | None,
    events: list[Event],
    journal_dir: Path,
) -> WriteResult:
    """Append events to a Cursor chat (creating it if needed) in one transaction, with an undo journal.

    Raises CursorBusyError when Cursor is running on the profile and CursorWriteError for read-only profiles.
    """
    if not profile.writable:
        raise CursorWriteError(f"profile '{profile.label}' is read-only (a backup); choose a live Cursor profile")
    if cursor_running(profile):
        raise CursorBusyError(f"Cursor is running on profile '{profile.label}'. Close Cursor, then try again.")
    tpl = _templates()
    ws_hash, workspace = workspace_for_folder(profile, folder)
    conn = sqlite3.connect(profile.db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_KEY + composer_id,)).fetchone()
        header_row = conn.execute("SELECT * FROM composerHeaders WHERE composerId = ?", (composer_id,)).fetchone()
        previous_composer = row[0] if row else None
        created = row is None
        first_ts = min((getattr(e, "ts_ms", 0) for e in events if getattr(e, "ts_ms", 0)), default=int(time.time() * 1000))
        composer = as_obj(json.loads(previous_composer)) if previous_composer else new_composer(tpl, composer_id, name, first_ts, workspace)
        headers = as_list(composer.get("fullConversationHeadersOnly"))
        floor = max((_ms(as_str(as_obj(h).get("createdAt"))) for h in headers), default=0)
        stamps = _spread_timestamps(events, floor)
        inserted: list[str] = []
        for event, ts in zip(events, stamps, strict=True):
            if isinstance(event, EmptyRecord):
                continue
            built = build_bubble(event, str(uuid.uuid4()), ts, tpl)
            if built is None:
                continue
            bubble, head = built
            key = f"{BUBBLE_KEY}{composer_id}:{head['bubbleId']}"
            conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (key, json.dumps(bubble, ensure_ascii=False)))
            inserted.append(key)
            headers.append(head)
        last_ts = max(stamps, default=first_ts)
        composer["fullConversationHeadersOnly"] = headers
        composer["lastUpdatedAt"] = max(int(as_obj(composer).get("lastUpdatedAt") or 0), last_ts)  # type: ignore[call-overload]
        composer["conversationCheckpointLastUpdatedAt"] = composer["lastUpdatedAt"]
        composer["status"] = "completed"
        composer["generatingBubbleIds"] = []
        journal = _write_journal(journal_dir, profile, composer_id, created, inserted, previous_composer, header_row)
        conn.execute(
            "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (COMPOSER_KEY + composer_id, json.dumps(composer, ensure_ascii=False)),
        )
        head_json = json.dumps(header_value(composer, workspace if created else _workspace_of(header_row, workspace)), ensure_ascii=False)
        created_at = int(composer["createdAt"])  # type: ignore[call-overload]
        updated = int(composer["lastUpdatedAt"])  # type: ignore[call-overload]
        conn.execute(
            "INSERT OR REPLACE INTO composerHeaders (composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, isSubagent, recency, "
            "checkpointAt, subagentTypeName, value) VALUES (?, ?, ?, ?, 0, 0, ?, ?, '', ?)",
            (
                composer_id,
                ws_hash if created else (header_row[1] if header_row else ws_hash),
                created_at,
                updated,
                updated,
                updated,
                head_json,
            ),
        )
        conn.execute("COMMIT")
        return WriteResult(composer_id, created, len(inserted), journal)
    except sqlite3.Error as exc:
        _rollback(conn)
        raise CursorWriteError(f"writing chat {composer_id} into {profile.db_path} failed: {exc}") from exc
    except BaseException:
        _rollback(conn)
        raise
    finally:
        conn.close()


def refile_chat(profile: CursorProfile, composer_id: str, folder: str, journal_dir: Path) -> WriteResult | None:
    """Move an existing chat into the workspace of `folder` (journaled, undoable).

    Used to repair chats that were filed under 'no folder' because the folder was unknown at the time. Returns None
    when there is nothing to do (chat missing, folder unresolvable, or already in that workspace).
    """
    if not profile.writable:
        raise CursorWriteError(f"profile '{profile.label}' is read-only (a backup); choose a live Cursor profile")
    if cursor_running(profile):
        raise CursorBusyError(f"Cursor is running on profile '{profile.label}'. Close Cursor, then try again.")
    ws_hash, workspace = workspace_for_folder(profile, folder)
    if ws_hash == "empty-window":
        return None
    conn = sqlite3.connect(profile.db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_KEY + composer_id,)).fetchone()
        header_row = conn.execute("SELECT * FROM composerHeaders WHERE composerId = ?", (composer_id,)).fetchone()
        if row is None or header_row is None or header_row[1] == ws_hash:
            conn.execute("ROLLBACK")
            return None
        composer = as_obj(json.loads(row[0]))
        composer["workspaceIdentifier"] = workspace
        head = as_obj(json.loads(str(header_row[-1])))
        head["workspaceIdentifier"] = workspace
        journal = _write_journal(journal_dir, profile, composer_id, False, [], row[0], header_row)
        conn.execute(
            "UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(composer, ensure_ascii=False), COMPOSER_KEY + composer_id)
        )
        conn.execute(
            "UPDATE composerHeaders SET workspaceId = ?, value = ? WHERE composerId = ?",
            (ws_hash, json.dumps(head, ensure_ascii=False), composer_id),
        )
        conn.execute("COMMIT")
        return WriteResult(composer_id, False, 0, journal)
    except sqlite3.Error as exc:
        _rollback(conn)
        raise CursorWriteError(f"re-filing chat {composer_id} failed: {exc}") from exc
    except BaseException:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _rollback(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.Error):  # no transaction open if the failure happened before BEGIN
        conn.execute("ROLLBACK")


def _ms(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _workspace_of(header_row: tuple[object, ...] | None, fallback: JsonObj) -> JsonObj:
    """Keep an existing chat's workspace identifier."""
    if header_row is None:
        return fallback
    try:
        return as_obj(as_obj(json.loads(str(header_row[-1]))).get("workspaceIdentifier")) or fallback
    except json.JSONDecodeError:
        return fallback


# --------------------------------------------------------------------------- journal / undo
def _write_journal(
    journal_dir: Path, profile: CursorProfile, composer_id: str, created: bool, inserted: list[str],
    previous_composer: object, header_row: tuple[object, ...] | None,
) -> Path:  # fmt: skip
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{composer_id}.json"
    payload = {
        "profile_db": str(profile.db_path), "profile_label": profile.label, "composer_id": composer_id, "created": created, "inserted_keys": inserted,
        "previous_composer": previous_composer if isinstance(previous_composer, str) else None,
        "previous_header_row": list(header_row) if header_row else None,
    }  # fmt: skip
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)
    return path


def undo_journal(profile: CursorProfile, journal: Path) -> int:
    """Revert one journaled write. Returns the number of bubble rows removed."""
    if cursor_running(profile):
        raise CursorBusyError(f"Cursor is running on profile '{profile.label}'. Close Cursor, then try again.")
    data = as_obj(json.loads(journal.read_text(encoding="utf-8")))
    composer_id = as_str(data.get("composer_id"))
    conn = sqlite3.connect(profile.db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        keys = [as_str(k) for k in as_list(data.get("inserted_keys"))]
        for key in keys:
            conn.execute("DELETE FROM cursorDiskKV WHERE key = ?", (key,))
        previous = data.get("previous_composer")
        if isinstance(previous, str):
            conn.execute("INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)", (COMPOSER_KEY + composer_id, previous))
        else:
            conn.execute("DELETE FROM cursorDiskKV WHERE key = ?", (COMPOSER_KEY + composer_id,))
        old_header = as_list(data.get("previous_header_row"))
        if old_header:
            conn.execute("INSERT OR REPLACE INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(old_header))
        else:
            conn.execute("DELETE FROM composerHeaders WHERE composerId = ?", (composer_id,))
        conn.execute("COMMIT")
        return len(keys)
    except BaseException:
        _rollback(conn)
        raise
    finally:
        conn.close()


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def is_chat_id(value: str) -> bool:
    """Whether a string looks like a Cursor chat id (uuid)."""
    return bool(_UUID_RE.match(value))
