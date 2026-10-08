"""Builds a complete synthetic Cursor + Claude environment in a temp directory.

Mirrors the real storage layout: state.vscdb (cursorDiskKV, composerHeaders), workspaceStorage,
agent-transcripts, plus the Claude project/session directories.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from chatbridge.config import AppPaths
from chatbridge.cursor_source import CursorProfile

CHAT_MAIN = "11111111-aaaa-4aaa-8aaa-000000000001"
CHAT_SUB = "22222222-aaaa-4aaa-8aaa-000000000002"
CHAT_EMPTY = "33333333-aaaa-4aaa-8aaa-000000000003"
CHAT_BACKUP_ONLY = "44444444-aaaa-4aaa-8aaa-000000000004"
CHAT_TRANSCRIPT = "55555555-aaaa-4aaa-8aaa-000000000005"
CREATED_MS = 1_788_000_000_000


@dataclass(frozen=True)
class World:
    """Handles to every directory of the synthetic environment."""

    root: Path
    project_dir: Path
    live_user: Path
    backup_user: Path
    paths: AppPaths


def bubble(kind: int, index: int, **fields: object) -> dict[str, object]:
    """A stored Cursor bubble with a deterministic timestamp."""
    return {"_v": 3, "type": kind, "bubbleId": f"b{index:03d}", "createdAt": f"2026-09-01T10:00:{index:02d}.000Z", **fields}


def _new_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    conn.execute(
        "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER,"
        " isArchived INTEGER, isSubagent INTEGER, recency INTEGER, checkpointAt INTEGER, subagentTypeName TEXT, value TEXT)"
    )
    return conn


def add_chat(
    conn: sqlite3.Connection,
    chat_id: str,
    name: str,
    bubbles: list[dict[str, object]],
    listed: list[int],
    workspace: str,
    folder: Path | None,
    subagent: bool = False,
) -> None:
    """Insert one chat. `listed` holds indexes of bubbles that appear in the header list (Cursor lists only some)."""
    headers = [
        {"bubbleId": bubbles[i]["bubbleId"], "type": bubbles[i]["type"], "grouping": {"textPreview": str(bubbles[i].get("text", ""))[:80]}}
        for i in listed
    ]
    data: dict[str, object] = {
        "composerId": chat_id,
        "name": name,
        "createdAt": CREATED_MS,
        "fullConversationHeadersOnly": headers,
    }
    if folder is not None:
        data["workspaceIdentifier"] = {"uri": {"fsPath": str(folder)}}
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{chat_id}", json.dumps(data)))
    for item in bubbles:
        conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{chat_id}:{item['bubbleId']}", json.dumps(item)))
    conn.execute(
        "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chat_id, workspace, CREATED_MS, None, 0, int(subagent), CREATED_MS, None, "", "{}"),
    )


def main_chat_bubbles() -> list[dict[str, object]]:
    """User, reasoning, tool call, tool error, text, image-only user, final text. Only some are in the header list."""
    ok_tool = {
        "toolCallId": "call-1\nfc_1",
        "name": "read_file_v2",
        "status": "completed",
        "params": json.dumps({"path": "/src/app.py"}),
        "result": json.dumps({"contents": "print('hi')"}),
    }
    bad_tool = {
        "toolCallId": "call-2",
        "name": "run_terminal_command_v2",
        "status": "error",
        "params": json.dumps({"command": "false"}),
        "error": "exit 1",
    }
    return [
        bubble(1, 0, text="Please fix the parser bug"),
        bubble(2, 1, thinking=json.dumps({"text": "Look at the parser first"})),
        bubble(2, 2, toolFormerData=ok_tool),
        bubble(2, 3, toolFormerData=bad_tool),
        bubble(2, 4, text="I found the issue and fixed it."),
        bubble(1, 5, text="", images=[{"id": "img1"}]),
        bubble(2, 6, text="Thanks, that screenshot confirms it."),
        bubble(2, 7),  # carries no content: must be accounted as an empty record
    ]


def build_world(root: Path) -> World:
    """Create the full environment under root and return its handles."""
    project = root / "projects" / "parser-app"
    project.mkdir(parents=True)
    live_user = root / "cursor" / "User"
    backup_user = root / "backup" / "User"
    (live_user / "workspaceStorage" / "ws1").mkdir(parents=True)
    (live_user / "workspaceStorage" / "ws1" / "workspace.json").write_text(json.dumps({"folder": f"file://{project}"}), encoding="utf-8")

    live = _new_db(live_user / "globalStorage" / "state.vscdb")
    add_chat(live, CHAT_MAIN, "Fix the parser", main_chat_bubbles(), [0, 4, 5, 6], "ws1", project)
    add_chat(
        live, CHAT_SUB, "Explore parser", [bubble(1, 0, text="explore"), bubble(2, 1, text="explored")], [0, 1], "ws1", None, subagent=True
    )
    add_chat(live, CHAT_EMPTY, "", [], [], "empty-window", None)
    live.commit()
    live.close()

    backup = _new_db(backup_user / "globalStorage" / "state.vscdb")
    # Same chat as live but with fewer records: discovery must keep the richer live copy.
    add_chat(backup, CHAT_MAIN, "Fix the parser", main_chat_bubbles()[:2], [0], "none", project)
    add_chat(
        backup,
        CHAT_BACKUP_ONLY,
        "Old windows chat",
        [bubble(1, 0, text="legacy question"), bubble(2, 1, text="legacy answer")],
        [0, 1],
        "none",
        Path("d:\\Work\\parser-app"),
    )
    backup.commit()
    backup.close()

    transcripts = root / ".cursor" / "projects" / "tmp-projects-parser-app" / "agent-transcripts" / CHAT_TRANSCRIPT
    transcripts.mkdir(parents=True)
    lines = [
        {
            "role": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<timestamp>Monday, Sep 14, 2026, 12:01 PM (UTC+5:30)</timestamp>\n<user_query>\ntranscript question\n</user_query>",
                    }
                ]
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [{"type": "text", "text": "transcript answer"}, {"type": "tool_use", "name": "Read", "input": {"path": "/x"}}]
            },
        },
        {"type": "turn_ended", "status": "success"},
    ]
    (transcripts / f"{CHAT_TRANSCRIPT}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    desktop = root / "claude-config" / "claude-code-sessions" / "org" / "acct"
    desktop.mkdir(parents=True)
    (desktop / "local_existing.json").write_text(
        json.dumps({"sessionId": "local_existing", "cliSessionId": "existing", "title": "my own chat"}), encoding="utf-8"
    )
    (root / "claude" / "projects").mkdir(parents=True)

    paths = AppPaths(
        claude_dir=root / "claude",
        desktop_dir=root / "claude-config" / "claude-code-sessions",
        data_dir=root / "data",
        config_dir=root / "config",
        transcripts_dir=root / ".cursor" / "projects",
        live_profile=CursorProfile(live_user, "live", writable=True),
    )
    return World(root, project, live_user, backup_user, paths)


# --------------------------------------------------------------------------- Claude-native sessions and follow-ups
CLAUDE_KEY = "local_aaaaaaaa-1111-4111-8111-000000000001"
CLAUDE_CLI = "bbbbbbbb-1111-4111-8111-000000000001"
T0 = 1_788_100_000_000


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


class ClaudeLog:
    """Builds entries shaped like the Claude desktop app's own (including harness noise that must be ignored)."""

    def __init__(self, session_id: str, cwd: str, parent: str | None = None, ts: int = T0) -> None:
        self.session_id, self.cwd, self.parent, self.ts = session_id, cwd, parent, ts
        self.lines: list[dict[str, object]] = []

    def _entry(self, kind: str, message: dict[str, object], **extra: object) -> str:
        self.ts += 1000
        uid = str(uuid.uuid4())
        self.lines.append({
            "parentUuid": self.parent, "isSidechain": False, "type": kind, "message": message, "uuid": uid, "timestamp": _iso(self.ts),
            "userType": "external", "entrypoint": "claude-desktop", "cwd": self.cwd, "sessionId": self.session_id, "version": "2.1.284", **extra,
        })  # fmt: skip
        self.parent = uid
        return uid

    def noise(self) -> None:
        self.lines.append({"type": "queue-operation", "operation": "enqueue", "sessionId": self.session_id})
        self.lines.append({"type": "attachment", "uuid": str(uuid.uuid4()), "parentUuid": self.parent, "sessionId": self.session_id})
        self._entry("user", {"role": "user", "content": "meta caveat"}, isMeta=True)
        self._entry("user", {"role": "user", "content": "side chain"}, isSidechain=True)

    def user(self, text: str, reminder: bool = False) -> None:
        blocks: list[dict[str, object]] = []
        if reminder:
            blocks.append(
                {"type": "text", "text": "<system-reminder>\nCodebase and user instructions are shown below.\n</system-reminder>"}
            )
        blocks.append({"type": "text", "text": text})
        self._entry("user", {"role": "user", "content": blocks})

    def assistant(self, text: str, thinking: str | None = None, tool: tuple[str, dict[str, object], str, bool] | None = None) -> None:
        blocks: list[dict[str, object]] = []
        if thinking:
            blocks.append({"type": "thinking", "thinking": thinking, "signature": "sig"})
        if text:
            blocks.append({"type": "text", "text": text})
        use_id = f"toolu_{uuid.uuid4().hex[:20]}"
        if tool:
            blocks.append({"type": "tool_use", "id": use_id, "name": tool[0], "input": tool[1]})
        self._entry("assistant", {"model": "claude-sonnet-5-5", "id": "msg_x", "type": "message", "role": "assistant", "content": blocks,
                                  "stop_reason": "tool_use" if tool else "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})  # fmt: skip
        if tool:
            result: dict[str, object] = {"type": "tool_result", "tool_use_id": use_id, "content": tool[2]}
            if tool[3]:
                result["is_error"] = True
            self._entry("user", {"role": "user", "content": [result]})


def write_claude_session(world: World, key: str, cli_id: str, title: str, log: ClaudeLog, cwd: str) -> Path:
    """Persist a ClaudeLog plus its desktop sidebar record; returns the log path."""
    slug = "".join(c if c.isalnum() else "-" for c in cwd)
    path = world.paths.claude_dir / "projects" / slug / f"{cli_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [*log.lines, {"type": "custom-title", "customTitle": title, "sessionId": cli_id}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    meta = {
        "sessionId": key,
        "cliSessionId": cli_id,
        "cwd": cwd,
        "createdAt": T0,
        "lastActivityAt": log.ts,
        "title": title,
        "completedTurns": 1,
    }
    (world.paths.desktop_dir / "org" / "acct" / f"{key}.json").write_text(json.dumps(meta), encoding="utf-8")
    age(path)
    return path


def age(path: Path, seconds: float = 60.0) -> None:
    """Pretend a file was last touched `seconds` ago (the sync refuses to append to a log changed moments ago)."""
    import os
    import time

    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


Turn = tuple[str, str] | tuple[str, str, tuple[str, dict[str, object], str, bool]]


def claude_follow_up(world: World, cli_id: str, turns: list[Turn]) -> Path:
    """Append native-like entries to a Claude log, as the app would when the user continues the chat.

    Each turn is ("u", text) for a human message or ("a", text[, tool]) for an assistant reply.
    """
    path = next((world.paths.claude_dir / "projects").glob(f"*/{cli_id}.jsonl"))
    last = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if '"uuid"' in line][-1]
    log = ClaudeLog(cli_id, str(last["cwd"]), str(last["uuid"]), ts=max(T0 + 500_000, _ms(str(last["timestamp"]))))
    for turn in turns:
        if turn[0] == "u":
            log.user(turn[1])
        else:
            log.assistant(turn[1], tool=turn[2] if len(turn) == 3 else None)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(line) + "\n" for line in log.lines))
    age(path)
    return path


def _ms(iso: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def cursor_follow_up(db_path: Path, chat_id: str, items: list[tuple[int, str]], start_ms: int) -> None:
    """Append plain user(1)/assistant(2) bubbles to a Cursor chat the way Cursor itself would."""
    conn = sqlite3.connect(db_path)
    data = json.loads(conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{chat_id}",)).fetchone()[0])
    for offset, (kind, text) in enumerate(items):
        stamp = start_ms + offset * 1000
        item = {"_v": 3, "type": kind, "bubbleId": f"f{stamp}", "createdAt": _iso(stamp), "text": text}
        conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{chat_id}:{item['bubbleId']}", json.dumps(item)))
        data["fullConversationHeadersOnly"].append(
            {"bubbleId": item["bubbleId"], "type": kind, "grouping": {"isRenderable": True}, "createdAt": item["createdAt"]}
        )
        data["lastUpdatedAt"] = stamp
    conn.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(data), f"composerData:{chat_id}"))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- Cursor agent state (model-facing conversation memory)
def add_native_agent_state(db_path: Path, chat_id: str) -> tuple[list[bytes], str]:
    """Give a native Cursor chat a model-facing state: [system prompt, environment message, one user turn]."""
    import base64
    import hashlib

    from chatbridge.agent_state import blob_bytes
    from chatbridge.protobuf import field_bytes

    messages: list[dict[str, object]] = [
        {"role": "system", "content": "You are an AI coding assistant, powered by Cursor Test Model. You operate in Cursor."},
        {
            "role": "user",
            "content": "<user_info>\nOS Version: linux\nWorkspace Path: unknown\n</user_info>",
            "providerOptions": {"cursor": {}},
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "<user_query>\nhello\n</user_query>"}],
            "providerOptions": {"cursor": {"requestId": "r"}},
        },
    ]
    conn = sqlite3.connect(db_path)
    hashes = []
    for message in messages:
        data = blob_bytes(message)
        digest = hashlib.sha256(data).digest()
        hashes.append(digest)
        conn.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)", (f"agentKv:blob:{digest.hex()}", data))
    state = "~" + base64.b64encode(b"".join(field_bytes(1, h) for h in hashes)).decode()
    data = json.loads(conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{chat_id}",)).fetchone()[0])
    data["conversationState"] = state
    conn.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(data), f"composerData:{chat_id}"))
    conn.commit()
    conn.close()
    return hashes, state
