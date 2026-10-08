"""Two-way sync between Cursor chats and Claude sessions.

Model
-----
A *conversation* is a Cursor chat and/or a Claude session. When both exist they are *paired* (via the link store, or
re-detected from deterministic ids). Syncing a pair is an append-only multiset merge of tool-neutral events:

  * events present in Cursor but missing in Claude are appended to the Claude log;
  * events present in Claude but missing in Cursor are appended to the Cursor chat;
  * nothing is ever rewritten or deleted on either side, and deletions never propagate;
  * a second sync right after a first one finds nothing missing (idempotent).

Time-based behaviour: when only one tool was continued, its new messages are appended to the other in their original
order (continue in Claude until 6pm, then in Cursor, then sync: each tool receives the other's newer messages). When both
were continued independently, each tool receives the other's new messages appended after its own, with the original
timestamps recorded in Cursor.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from .catalog import ChatRow, project_label
from .claude_source import ClaudeSession, iter_claude_events, list_claude_sessions
from .config import AppPaths, Settings
from .converter import NO_ERROR_DETAIL_TEXT, NO_OUTPUT_TEXT
from .cursor_source import BUBBLE_PREFIX, COMPOSER_PREFIX, CursorProfile, iter_events, key_range, open_readonly
from .cursor_writer import (
    CursorBusyError,
    cursor_chat_id_for_claude,
    refile_chat,
    undo_journal,
    upsert_events,
    workspace_for_folder,
)
from .events import meaningful, missing_events
from .links import Link, LinkStore, Origin
from .model import (
    AssistantText,
    ChatRef,
    Event,
    ImporterError,
    Reasoning,
    ToolCall,
    UserText,
    as_int,
    as_list,
    as_obj,
    as_str,
)
from .service import ImportService, PreviewLine
from .writer import ClaudeBusyError, append_events_to_claude, find_sessions_dir_or_none, import_chat, local_session_name, session_uuid

LOG = logging.getLogger(__name__)
Direction = Literal["both", "to-claude", "to-cursor"]
DIRTY = "dirty"


class SyncState(Enum):
    """Where a conversation stands relative to its counterpart."""

    CURSOR_ONLY = "Cursor only"
    CLAUDE_ONLY = "Claude only"
    IN_SYNC = "In sync"
    CURSOR_CHANGED = "Cursor has new messages"
    CLAUDE_CHANGED = "Claude has new messages"
    BOTH_CHANGED = "Both changed"
    UNCHECKED = "Linked, not yet compared"
    BROKEN = "Counterpart missing"


@dataclass(frozen=True)
class Conversation:
    """One row of the unified list."""

    key: str
    title: str
    cursor: ChatRow | None
    claude: ClaudeSession | None
    link: Link | None
    state: SyncState

    @property
    def project(self) -> str:
        cwd = self.claude.cwd if self.claude and self.claude.cwd else (self.cursor.ref.cwd if self.cursor else None)
        return project_label(cwd)

    @property
    def updated_ms(self) -> int:
        cursor = self.cursor.ref.updated_ms or self.cursor.ref.created_ms if self.cursor else 0
        return max(cursor, self.claude.updated_ms if self.claude else 0)

    @property
    def is_subagent(self) -> bool:
        return bool(self.cursor and self.cursor.ref.is_subagent)

    @property
    def is_empty(self) -> bool:
        return (self.cursor is None or self.cursor.is_empty) and (self.claude is None or self.claude.record_count == 0)


@dataclass
class SyncReport:
    """Result of syncing one conversation."""

    key: str
    title: str
    status: Literal["synced", "noop", "deferred", "failed", "dry-run"]
    to_claude: int = 0
    to_cursor: int = 0
    detail: str = ""
    journal: str = ""
    notes: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class JournalEntry:
    """One reversible write into a Cursor database."""

    path: Path
    chat_id: str
    created: bool
    messages: int
    stamp: str
    profile: str


@dataclass(frozen=True)
class SyncProgress:
    done: int
    total: int
    report: SyncReport


def cursor_fingerprint(row: ChatRow) -> str:
    """Cheap change detector for a Cursor chat (stored record count + last update)."""
    if row.ref.kind == "transcript":
        try:
            return f"{row.ref.source_path.stat().st_size}:{row.ref.updated_ms}"
        except OSError:
            return ""
    return f"{row.ref.record_count}:{row.ref.updated_ms}"


def ref_for_link(db_path: str, chat_id: str) -> ChatRef:
    """A minimal ChatRef for a stored link (transcript files end in .jsonl, everything else is a database)."""
    kind: Literal["db", "transcript"] = "transcript" if db_path.endswith(".jsonl") else "db"
    return ChatRef(chat_id, "", 0, None, False, kind, Path(db_path), "", 0)


def fresh_cursor_fingerprint(ref: ChatRef) -> str:
    """Fingerprint recomputed from the source right now (database rows, or transcript size/mtime)."""
    if ref.kind == "transcript":
        try:
            stat = ref.source_path.stat()
        except OSError:
            return ""
        return f"{stat.st_size}:{int(stat.st_mtime * 1000)}"
    conn = open_readonly(ref.source_path)
    try:
        low, high = key_range(f"{BUBBLE_PREFIX}{ref.chat_id}:")
        count = conn.execute("SELECT count(*) FROM cursorDiskKV WHERE key >= ? AND key < ?", (low, high)).fetchone()[0]
        row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (COMPOSER_PREFIX + ref.chat_id,)).fetchone()
        updated = as_int(as_obj(json.loads(row[0])).get("lastUpdatedAt")) if row else 0
        return f"{count}:{updated}"
    finally:
        conn.close()


def _clean_output(call: ToolCall) -> ToolCall:
    """Drop placeholder outputs written by ChatBridge so they are never mistaken for real tool output."""
    if call.output in (NO_OUTPUT_TEXT, NO_ERROR_DETAIL_TEXT):
        return ToolCall(call.seq, call.call_id, call.name, call.tool_input, None, call.is_error, call.ts_ms)
    return call


def read_cursor_events(ref: ChatRef) -> list[Event]:
    return meaningful(iter_events(ref))


def read_claude_events(session: ClaudeSession) -> list[Event]:
    return [_clean_output(e) if isinstance(e, ToolCall) else e for e in meaningful(iter_claude_events(session))]


class SyncService:
    """Orchestrates catalog pairing, planning and applying syncs. Thread-safe: writes are serialised."""

    def __init__(self, paths: AppPaths, settings: Settings, service: ImportService | None = None) -> None:
        self.paths, self.settings = paths, settings
        self.service = service or ImportService(paths, settings)
        self.links = LinkStore(paths.links_db)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ catalog
    def profiles(self) -> list[CursorProfile]:
        return self.service.profiles()

    def writable_profiles(self) -> list[CursorProfile]:
        return [p for p in self.profiles() if p.writable and p.db_path.is_file()]

    def profile_for(self, db_path: str) -> CursorProfile | None:
        return next((p for p in self.profiles() if str(p.db_path) == db_path), None)

    def load_conversations(self) -> tuple[list[Conversation], list[str]]:
        """Scan both tools and pair what belongs together."""
        rows, warnings = self.service.load_catalog()
        sessions = list_claude_sessions(self.paths)
        by_cursor = {(str(r.ref.source_path), r.ref.chat_id): r for r in rows}
        by_id: dict[str, ChatRow] = {}
        for row in rows:
            by_id.setdefault(row.ref.chat_id, row)
        claude_by_key = {s.key: s for s in sessions}
        used_cursor: set[tuple[str, str]] = set()
        used_claude: set[str] = set()
        conversations: list[Conversation] = []

        def add(cursor: ChatRow | None, claude: ClaudeSession | None, link: Link | None, origin: Origin) -> None:
            if cursor is not None:
                used_cursor.add((str(cursor.ref.source_path), cursor.ref.chat_id))
            if claude is not None:
                used_claude.add(claude.key)
            title = claude.title if claude and origin == "claude" else cursor.title if cursor else claude.title if claude else ""
            key = f"k:{claude.key}" if claude else f"c:{cursor.ref.source_path}:{cursor.ref.chat_id}" if cursor else "?"
            conversations.append(Conversation(key, title, cursor, claude, link, self._state(cursor, claude, link)))

        for link in self.links.all():
            cursor = by_cursor.get((link.cursor_db, link.cursor_chat_id))
            claude = claude_by_key.get(link.claude_key)
            if cursor or claude:
                add(cursor, claude, link, link.origin)
        for session in sessions:
            if session.key in used_claude:
                continue
            cursor_id = cursor_chat_id_for_claude(session.key)
            if cursor_id in by_id:
                add(by_id[cursor_id], session, None, "claude")
        for row in rows:
            if (str(row.ref.source_path), row.ref.chat_id) in used_cursor:
                continue
            match = claude_by_key.get(local_session_name(row.ref.chat_id)) or next(
                (s for s in sessions if s.cli_id == session_uuid(row.ref.chat_id)), None
            )
            if match is not None and match.key not in used_claude:
                add(row, match, None, "cursor")
        for row in rows:
            if (str(row.ref.source_path), row.ref.chat_id) not in used_cursor:
                add(row, None, None, "cursor")
        for session in sessions:
            if session.key not in used_claude:
                add(None, session, None, "claude")
        return sorted(conversations, key=lambda c: c.updated_ms, reverse=True), warnings

    @staticmethod
    def _state(cursor: ChatRow | None, claude: ClaudeSession | None, link: Link | None) -> SyncState:
        if cursor is None and claude is None:
            return SyncState.BROKEN
        if claude is None:
            return SyncState.BROKEN if link else SyncState.CURSOR_ONLY
        if cursor is None:
            return SyncState.BROKEN if link else SyncState.CLAUDE_ONLY
        if link is None or not link.last_sync_ms:
            return SyncState.UNCHECKED
        cursor_changed = cursor_fingerprint(cursor) != link.cursor_fp
        claude_changed = claude.fingerprint != link.claude_fp
        if cursor_changed and claude_changed:
            return SyncState.BOTH_CHANGED
        if cursor_changed:
            return SyncState.CURSOR_CHANGED
        if claude_changed:
            return SyncState.CLAUDE_CHANGED
        return SyncState.IN_SYNC

    # ------------------------------------------------------------------ planning
    def plan(self, conversation: Conversation) -> tuple[list[Event], list[Event]]:
        """(events to append to Claude, events to append to Cursor) from an exact event-level comparison."""
        cursor_events = read_cursor_events(conversation.cursor.ref) if conversation.cursor else []
        claude_events = read_claude_events(conversation.claude) if conversation.claude else []
        return missing_events(cursor_events, claude_events), missing_events(claude_events, cursor_events)

    # ------------------------------------------------------------------ applying
    def sync(
        self,
        conversation: Conversation,
        direction: Direction = "both",
        apply: bool = False,
        target_profile: CursorProfile | None = None,
        cwd_override: str | None = None,
    ) -> SyncReport:
        """Sync (or create the missing counterpart of) one conversation. A failure is returned, never raised."""
        report = SyncReport(conversation.key, conversation.title, "dry-run" if not apply else "noop")
        try:
            with self._lock:
                if conversation.cursor and conversation.claude:
                    self._sync_pair(conversation, direction, apply, report)
                elif conversation.cursor:
                    self._create_claude_side(conversation, direction, apply, cwd_override, report)
                elif conversation.claude:
                    self._create_cursor_side(conversation, direction, apply, target_profile, report)
                else:
                    report.status, report.detail = "failed", "conversation has no readable side"
        except (ImporterError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            LOG.exception("sync failed for %s", conversation.key)
            report.status, report.detail = "failed", f"{type(exc).__name__}: {exc}"
        return report

    def _sync_pair(self, conv: Conversation, direction: Direction, apply: bool, report: SyncReport) -> None:
        assert conv.cursor and conv.claude
        to_claude, to_cursor = self.plan(conv)
        if direction == "to-claude":
            to_cursor = []
        if direction == "to-cursor":
            to_claude = []
        if conv.cursor.ref.kind == "transcript" and to_cursor:
            to_cursor = []
            report.notes.append("Cursor transcript files are read-only: messages can flow Cursor -> Claude only.")
        report.to_claude, report.to_cursor = len(to_claude), len(to_cursor)
        refile = self._refile_target(conv)
        if refile is not None:
            report.fixes.append(f"move the Cursor chat into the project folder {refile[1]} (it is filed under 'no folder')")
        if not to_claude and not to_cursor and refile is None:
            report.status = "noop" if apply else "dry-run"
            self._remember(conv, conv.link)
            return
        if not apply:
            return
        deferred: list[str] = []
        if refile is not None:
            try:
                moved = refile_chat(refile[0], conv.cursor.ref.chat_id, refile[1], self.paths.journal_dir)
                report.journal = str(moved.journal_path) if moved and moved.journal_path else report.journal
            except (CursorBusyError, ImporterError) as exc:
                deferred.append(str(exc))
        cursor_done = claude_done = True
        if to_claude:
            try:
                append_events_to_claude(conv.claude.log_path, conv.claude.meta_path, conv.cursor.ref.chat_id, to_claude)
            except ClaudeBusyError as exc:
                claude_done = False
                deferred.append(str(exc))
        if to_cursor:
            profile = self.profile_for(str(conv.cursor.ref.source_path))
            try:
                if profile is None:
                    raise CursorBusyError("the Cursor profile of this chat is not configured")
                result = upsert_events(profile, conv.cursor.ref.chat_id, conv.title, conv.cursor.ref.cwd, to_cursor, self.paths.journal_dir)
                report.journal = str(result.journal_path or "")
            except (CursorBusyError, ImporterError) as exc:
                cursor_done = False
                deferred.append(str(exc))
        report.status = "deferred" if deferred else "synced"
        report.detail = " ".join(deferred)
        # A side whose new events could not be delivered stays marked dirty so it keeps showing as changed.
        self._remember(conv, conv.link, claude_dirty=bool(to_cursor) and not cursor_done, cursor_dirty=bool(to_claude) and not claude_done)

    def _refile_target(self, conv: Conversation) -> tuple[CursorProfile, str] | None:
        """(profile, folder) when a ChatBridge-created Cursor chat sits in 'no folder' but its folder is now resolvable."""
        if not (conv.link and conv.link.origin == "claude" and conv.cursor and conv.claude):
            return None
        if conv.cursor.ref.kind != "db" or conv.cursor.ref.cwd or not conv.claude.cwd or not Path(conv.claude.cwd).is_dir():
            return None
        profile = self.profile_for(str(conv.cursor.ref.source_path))
        if profile is None or not profile.writable:
            return None
        workspace_id, _ = workspace_for_folder(profile, conv.claude.cwd)
        return (profile, conv.claude.cwd) if workspace_id != "empty-window" else None

    def _create_claude_side(self, conv: Conversation, direction: Direction, apply: bool, cwd: str | None, report: SyncReport) -> None:
        assert conv.cursor
        if direction == "to-cursor":
            report.status, report.detail = "noop", "chat exists only in Cursor and direction is Claude -> Cursor"
            return
        ref = conv.cursor.ref
        events = read_cursor_events(ref)
        report.to_claude = len(events)
        if not apply:
            return
        sessions_dir = find_sessions_dir_or_none(self.paths.desktop_dir)
        result = import_chat(ref, lambda: iter_events(ref), self.paths.claude_dir, sessions_dir, True, self.service.aliases, cwd)
        if result.status not in ("written", "exists"):
            raise ImporterError(result.detail or f"import status {result.status}")
        report.status = "synced"
        report.detail = result.detail
        key = local_session_name(ref.chat_id) if sessions_dir is not None else session_uuid(ref.chat_id)
        self._link_after_create(conv, origin="cursor", claude_key=key, cursor_row=conv.cursor)

    def _create_cursor_side(
        self, conv: Conversation, direction: Direction, apply: bool, profile: CursorProfile | None, report: SyncReport
    ) -> None:
        assert conv.claude
        if direction == "to-claude":
            report.status, report.detail = "noop", "session exists only in Claude and direction is Cursor -> Claude"
            return
        events = read_claude_events(conv.claude)
        report.to_cursor = len(events)
        if not apply:
            return
        target = profile or next(iter(self.writable_profiles()), None)
        if target is None:
            raise ImporterError("no writable Cursor profile found; add one in settings")
        composer_id = cursor_chat_id_for_claude(conv.claude.key)
        result = upsert_events(target, composer_id, conv.claude.title, conv.claude.cwd, events, self.paths.journal_dir)
        report.journal = str(result.journal_path or "")
        report.status = "synced"
        link = Link(str(target.db_path), composer_id, conv.claude.key, "claude", int(time.time() * 1000))
        self.links.save(link)
        self._finish_link(link, composer_id, str(target.db_path), conv.claude)

    # ------------------------------------------------------------------ link bookkeeping
    def _link_after_create(self, conv: Conversation, origin: Origin, claude_key: str, cursor_row: ChatRow) -> None:
        link = Link(str(cursor_row.ref.source_path), cursor_row.ref.chat_id, claude_key, origin, int(time.time() * 1000))
        self.links.save(link)
        session = next((s for s in list_claude_sessions(self.paths) if s.key == claude_key), None)
        if session is not None:
            self._finish_link(link, cursor_row.ref.chat_id, str(cursor_row.ref.source_path), session)

    def _finish_link(self, link: Link, chat_id: str, db_path: str, session: ClaudeSession) -> None:
        fp = self._cursor_fp_now(db_path, chat_id)
        self.links.record_sync(link, fp, session.fingerprint)

    def _cursor_fp_now(self, db_path: str, chat_id: str) -> str:
        return fresh_cursor_fingerprint(ref_for_link(db_path, chat_id))

    def _remember(self, conv: Conversation, link: Link | None, claude_dirty: bool = False, cursor_dirty: bool = False) -> None:
        """Persist the pairing and fingerprints after a sync attempt (a side with undelivered events is stored as dirty)."""
        assert conv.cursor and conv.claude
        base = link or Link(
            str(conv.cursor.ref.source_path), conv.cursor.ref.chat_id, conv.claude.key, self._origin_of(conv), int(time.time() * 1000)
        )
        db, chat_id = str(conv.cursor.ref.source_path), conv.cursor.ref.chat_id
        session = next((s for s in list_claude_sessions(self.paths) if s.key == conv.claude.key), conv.claude)
        cursor_fp = DIRTY if cursor_dirty else self._cursor_fp_now(db, chat_id)
        claude_fp = DIRTY if claude_dirty else session.fingerprint
        self.links.record_sync(base, cursor_fp, claude_fp)

    @staticmethod
    def _origin_of(conv: Conversation) -> Origin:
        assert conv.claude
        return "cursor" if conv.claude.key == local_session_name(conv.cursor.ref.chat_id if conv.cursor else "") else "claude"

    # ------------------------------------------------------------------ batches
    def sync_many(
        self,
        conversations: list[Conversation],
        direction: Direction = "both",
        apply: bool = False,
        on_progress: Callable[[SyncProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        target_profile: CursorProfile | None = None,
    ) -> list[SyncReport]:
        reports: list[SyncReport] = []
        for index, conversation in enumerate(conversations, start=1):
            if should_cancel and should_cancel():
                break
            report = self.sync(conversation, direction, apply, target_profile)
            reports.append(report)
            if on_progress:
                on_progress(SyncProgress(index, len(conversations), report))
        return reports

    def linked_changed(self) -> list[Conversation]:
        """Linked conversations whose fingerprints changed since the last sync (what auto-sync should process)."""
        conversations, _ = self.load_conversations()
        changed = {SyncState.CURSOR_CHANGED, SyncState.CLAUDE_CHANGED, SyncState.BOTH_CHANGED, SyncState.UNCHECKED}
        return [c for c in conversations if c.link and c.link.auto_sync and c.state in changed and c.cursor and c.claude]

    def preview(self, conversation: Conversation, limit: int = 40) -> list[PreviewLine]:
        """First messages of a conversation (from Cursor if present, else from Claude)."""
        if conversation.cursor is not None:
            return self.service.preview(conversation.cursor.ref, limit)
        if conversation.claude is None:
            return []
        lines: list[PreviewLine] = []
        for event in read_claude_events(conversation.claude):
            if isinstance(event, UserText):
                lines.append(PreviewLine("You", event.text[:1200]))
            elif isinstance(event, AssistantText):
                lines.append(PreviewLine("Assistant", event.text[:1200]))
            elif isinstance(event, Reasoning):
                lines.append(PreviewLine("Reasoning", event.text[:1200]))
            elif isinstance(event, ToolCall):
                lines.append(PreviewLine("Tool", event.name))
            if len(lines) >= limit:
                break
        return lines

    def list_journals(self) -> list[JournalEntry]:
        """Undo journals of writes into Cursor, newest first (already-undone ones are not listed)."""
        entries: list[JournalEntry] = []
        for path in sorted(self.paths.journal_dir.glob("*.json"), reverse=True):
            try:
                data = as_obj(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
            stamp = path.name[:15]
            label = as_str(data.get("profile_label")) or Path(as_str(data.get("profile_db"))).parent.parent.parent.name
            entries.append(
                JournalEntry(
                    path,
                    as_str(data.get("composer_id")),
                    data.get("created") is True,
                    len(as_list(data.get("inserted_keys"))),
                    stamp,
                    label,
                )
            )
        return entries

    def undo_cursor_write(self, journal: Path) -> int:
        """Revert one journaled Cursor write; if it had created the chat, the now-dangling link is dropped too."""
        data = as_obj(json.loads(journal.read_text(encoding="utf-8")))
        db_path = as_str(data.get("profile_db"))
        profile = self.profile_for(db_path) or CursorProfile(Path(db_path).parent.parent, "journal", writable=True)
        removed = undo_journal(profile, journal)
        if data.get("created") is True:
            for link in self.links.all():
                if link.cursor_db == db_path and link.cursor_chat_id == as_str(data.get("composer_id")):
                    self.links.remove(link.claude_key)
        journal.rename(journal.with_suffix(".undone"))
        return removed

    def unlink(self, conversation: Conversation) -> None:
        """Stop tracking a pair (both conversations stay as they are)."""
        if conversation.claude:
            self.links.remove(conversation.claude.key)
