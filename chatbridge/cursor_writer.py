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

from .agent_state import BLOB_KEY, build_state, decode_state, find_prefix
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
RECENTS_KEY = "history.recentlyOpenedPathsList"
CHARS_PER_TOKEN = 4


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
    context_carried: bool | None = None  # None: not requested; False: requested but impossible (see notes)


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
        "unifiedMode": "agent", "forceMode": "edit", "hasUnreadMessages": False, "contextUsagePercent": composer.get("contextUsagePercent", 0), "totalLinesAdded": 0,
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
    carry_context: bool = False,
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
        added_tokens = estimate_tokens(events)
        carried: bool | None = None
        state_tokens: int | None = None
        if carry_context:
            state_tokens = _store_agent_state(conn, composer, composer_id, events, added_tokens, inserted, rebuild=created)
            carried = state_tokens is not None
        if created and state_tokens is not None:
            _set_context(composer, state_tokens)  # the meter matches what the model will be sent
        else:
            _set_context(composer, int(composer.get("contextTokensUsed") or 0) + added_tokens)  # type: ignore[call-overload]
        recents = _put_recents(conn, folder) if created and ws_hash != "empty-window" else (False, None)
        journal = _write_journal(journal_dir, profile, composer_id, created, inserted, previous_composer, header_row, recents)
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
        return WriteResult(composer_id, created, len(inserted), journal, carried)
    except sqlite3.Error as exc:
        _rollback(conn)
        raise CursorWriteError(f"writing chat {composer_id} into {profile.db_path} failed: {exc}") from exc
    except BaseException:
        _rollback(conn)
        raise
    finally:
        conn.close()


def repair_chat(
    profile: CursorProfile,
    composer_id: str,
    folder: str | None,
    context_tokens: int | None,
    journal_dir: Path,
    carry_events: list[Event] | None = None,
) -> WriteResult | None:
    """Fix metadata of a ChatBridge-created chat in one journaled transaction (undoable).

    * file it under the workspace of `folder` if it sits in 'no folder' (folder was unknown when it was created);
    * add `folder` to Cursor's recent projects if missing;
    * set the context-usage estimate if the stored figure is 0;
    * (experimental) give the chat a model-facing conversation state built from `carry_events` if it has none.

    Returns None when there is nothing to do.
    """
    if not profile.writable:
        raise CursorWriteError(f"profile '{profile.label}' is read-only (a backup); choose a live Cursor profile")
    if cursor_running(profile):
        raise CursorBusyError(f"Cursor is running on profile '{profile.label}'. Close Cursor, then try again.")
    ws_hash, workspace = workspace_for_folder(profile, folder)
    conn = sqlite3.connect(profile.db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_KEY + composer_id,)).fetchone()
        header_row = conn.execute("SELECT * FROM composerHeaders WHERE composerId = ?", (composer_id,)).fetchone()
        if row is None or header_row is None:
            conn.execute("ROLLBACK")
            return None
        composer = as_obj(json.loads(row[0]))
        head = as_obj(json.loads(str(header_row[-1])))
        refile = ws_hash != "empty-window" and header_row[1] != ws_hash
        set_context = context_tokens is not None and int(composer.get("contextTokensUsed") or 0) == 0  # type: ignore[call-overload]
        recents_previous, recents_new = _recents_with_folder(conn, folder) if folder and Path(folder).is_dir() else (None, None)
        carry = carry_events is not None and decode_state(as_str(composer.get("conversationState"))) is None
        if not (refile or set_context or recents_new is not None or carry):
            conn.execute("ROLLBACK")
            return None
        inserted: list[str] = []
        carried: bool | None = None
        state_tokens: int | None = None
        if carry and carry_events is not None:
            state_tokens = _store_agent_state(
                conn, composer, composer_id, carry_events, estimate_tokens(carry_events), inserted, rebuild=True
            )
            carried = state_tokens is not None
        journal = _write_journal(
            journal_dir, profile, composer_id, False, inserted, row[0], header_row, (recents_new is not None, recents_previous)
        )
        if refile:
            composer["workspaceIdentifier"] = workspace
            head["workspaceIdentifier"] = workspace
        if state_tokens is not None:
            _set_context(composer, state_tokens)
            head["contextUsagePercent"] = composer["contextUsagePercent"]
        elif set_context and context_tokens is not None:
            _set_context(composer, context_tokens)
            head["contextUsagePercent"] = composer["contextUsagePercent"]
        conn.execute(
            "UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(composer, ensure_ascii=False), COMPOSER_KEY + composer_id)
        )
        conn.execute(
            "UPDATE composerHeaders SET workspaceId = ?, value = ? WHERE composerId = ?",
            (ws_hash if refile else header_row[1], json.dumps(head, ensure_ascii=False), composer_id),
        )
        if recents_new is not None:
            conn.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (RECENTS_KEY, recents_new))
        conn.execute("COMMIT")
        return WriteResult(composer_id, False, 0, journal, carried)
    except sqlite3.Error as exc:
        _rollback(conn)
        raise CursorWriteError(f"repairing chat {composer_id} failed: {exc}") from exc
    except BaseException:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _store_agent_state(
    conn: sqlite3.Connection,
    composer: JsonObj,
    composer_id: str,
    events: list[Event],
    token_estimate: int,
    inserted: list[str],
    *,
    rebuild: bool,
) -> int | None:
    """Give the chat a model-facing conversation state (EXPERIMENTAL). Returns its token estimate, or None if it cannot be built.

    rebuild=True builds the state from `events` (the whole conversation); otherwise `events` are appended to the
    chat's existing state. New blob rows are added to `inserted` so Undo can remove them.
    """
    existing = None if rebuild else decode_state(as_str(composer.get("conversationState")))
    if not rebuild and existing is None:
        return None
    prefix = find_prefix(conn, {composer_id}) if existing is None else []
    if existing is None and prefix is None:
        return None
    limit = int(composer.get("contextTokenLimit") or CONTEXT_LIMIT)  # type: ignore[call-overload]
    budget = None if existing is not None else max(10_000, limit // 2 - sum(len(b) for b in prefix or []) // CHARS_PER_TOKEN)
    built = build_state(prefix or [], existing, events, limit, token_estimate, budget)
    for digest, data in built.blobs.items():
        key = BLOB_KEY + digest
        if conn.execute("SELECT 1 FROM cursorDiskKV WHERE key = ?", (key,)).fetchone() is None:
            conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (key, data))
            inserted.append(key)
    composer["conversationState"] = built.state
    return built.tokens


def chat_state_is_empty(profile: CursorProfile, composer_id: str) -> bool | None:
    """Whether the chat has no model-facing conversation state yet (None if the chat is missing)."""
    try:
        conn = sqlite3.connect(f"file:{profile.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_KEY + composer_id,)).fetchone()
        return decode_state(as_str(as_obj(json.loads(row[0])).get("conversationState"))) is None if row else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None
    finally:
        conn.close()


def prefix_available(profile: CursorProfile) -> bool:
    """Whether the profile has a native chat whose system prompt can be borrowed."""
    try:
        conn = sqlite3.connect(f"file:{profile.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return find_prefix(conn) is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def estimate_tokens(events: list[Event]) -> int:
    """Rough token count of events (about 4 characters per token), used for Cursor's context-usage meter.

    It is an estimate, not a tokenizer: Cursor replaces it with the real figure after the next message in the chat.
    """
    chars = 0
    for event in events:
        if isinstance(event, (UserText, AssistantText, Reasoning)):
            chars += len(event.text)
        elif isinstance(event, ToolCall):
            chars += len(json.dumps(event.tool_input, ensure_ascii=False)) + len(event.output or "")
    return chars // CHARS_PER_TOKEN


def _set_context(composer: JsonObj, tokens: int) -> None:
    """Record `tokens` as the chat's context usage (all three places Cursor reads it)."""
    limit = int(composer.get("contextTokenLimit") or CONTEXT_LIMIT)  # type: ignore[call-overload]
    composer["contextTokensUsed"] = tokens
    composer["contextTokenLimit"] = limit
    composer["contextUsagePercent"] = round(min(100.0, tokens * 100 / limit), 3)
    composer["promptTokenBreakdown"] = {"totalUsedTokens": tokens, "maxTokens": limit, "categories": []}


def _recents_with_folder(conn: sqlite3.Connection, folder: str) -> tuple[str | None, str | None]:
    """(previous value, new value) of Cursor's recent-projects list with `folder` added at the front; new is None if unchanged."""
    row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (RECENTS_KEY,)).fetchone()
    previous = row[0] if row else None
    parsed = as_obj(json.loads(previous)) if isinstance(previous, str) and previous else {}
    entries = as_list(parsed.get("entries"))
    uri = f"file://{folder}"
    if any(as_str(as_obj(entry).get("folderUri")) == uri for entry in entries):
        return previous if isinstance(previous, str) else None, None
    parsed["entries"] = [{"folderUri": uri}, *entries]
    return previous if isinstance(previous, str) else None, json.dumps(parsed, ensure_ascii=False)


def _put_recents(conn: sqlite3.Connection, folder: str | None) -> tuple[bool, str | None]:
    """Add the project folder to Cursor's recent projects. Returns (changed, previous value) for the journal."""
    if not folder or not Path(folder).is_dir():
        return False, None
    previous, new = _recents_with_folder(conn, folder)
    if new is None:
        return False, None
    conn.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (RECENTS_KEY, new))
    return True, previous


def folder_in_recents(profile: CursorProfile, folder: str) -> bool:
    """Whether the folder is already in the profile's recent projects (read-only check)."""
    try:
        conn = sqlite3.connect(f"file:{profile.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return True
    try:
        return _recents_with_folder(conn, folder)[1] is None
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def chat_context_tokens(profile: CursorProfile, composer_id: str) -> int | None:
    """Stored context-token figure of a chat (None if the chat is missing)."""
    try:
        conn = sqlite3.connect(f"file:{profile.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_KEY + composer_id,)).fetchone()
        return int(as_obj(json.loads(row[0])).get("contextTokensUsed") or 0) if row else None  # type: ignore[call-overload]
    except (sqlite3.Error, ValueError):
        return None
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
    previous_composer: object, header_row: tuple[object, ...] | None, recents: tuple[bool, str | None] = (False, None),
) -> Path:  # fmt: skip
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{composer_id}.json"
    payload = {
        "profile_db": str(profile.db_path), "profile_label": profile.label, "composer_id": composer_id, "created": created,
        "inserted_keys": inserted, "previous_composer": previous_composer if isinstance(previous_composer, str) else None,
        "previous_header_row": list(header_row) if header_row else None,
        "recents": {"changed": recents[0], "previous": recents[1]},
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
        recents = as_obj(data.get("recents"))
        if recents.get("changed") is True:
            previous_recents = recents.get("previous")
            if isinstance(previous_recents, str):
                conn.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (RECENTS_KEY, previous_recents))
            else:
                conn.execute("DELETE FROM ItemTable WHERE key = ?", (RECENTS_KEY,))
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
