"""Keeps linked conversations up to date automatically.

Every tick does a cheap fingerprint check of all auto-synced links (one indexed query + one stat per link). Only
when something changed is the full catalog loaded and the affected conversations synced. Rules that keep it safe:

  * only already-linked conversations are touched; nothing new is ever imported automatically;
  * Cursor-side writes wait while Cursor is running on that profile (Claude-side writes still go through);
  * a Claude log changed within the last few seconds is left alone (the app may be mid-turn);
  * deferred work is retried on later ticks, without re-reading chats while nothing changed.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .claude_source import list_claude_sessions
from .cursor_writer import cursor_running
from .model import ImporterError
from .sync import DIRTY, SyncReport, SyncService, fresh_cursor_fingerprint, ref_for_link

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoSyncEvent:
    """Something the auto-syncer wants to tell the UI."""

    kind: str  # "synced" | "deferred" | "failed" | "idle"
    message: str
    reports: list[SyncReport] = field(default_factory=list)


class AutoSyncer:
    """Polling auto-sync. Use run_once() for a single pass or start()/stop() for a background thread."""

    def __init__(self, sync: SyncService, interval_seconds: float = 20.0, on_event: Callable[[AutoSyncEvent], None] | None = None) -> None:
        self.sync = sync
        self.interval = interval_seconds
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_attempt: dict[str, tuple[str, str, bool]] = {}

    def changed_keys(self) -> set[str]:
        """Claude keys of linked conversations whose fingerprints differ from the stored ones (cheap check)."""
        sessions = {s.key: s for s in list_claude_sessions(self.sync.paths)}
        changed: set[str] = set()
        for link in self.sync.links.all():
            if not link.auto_sync:
                continue
            session = sessions.get(link.claude_key)
            if session is None:
                continue
            try:
                cursor_fp = fresh_cursor_fingerprint(ref_for_link(link.cursor_db, link.cursor_chat_id))
            except (ImporterError, OSError, sqlite3.Error):
                continue
            if DIRTY in (link.cursor_fp, link.claude_fp) or cursor_fp != link.cursor_fp or session.fingerprint != link.claude_fp:
                changed.add(link.claude_key)
        return changed

    def run_once(self) -> list[SyncReport]:
        """One pass: sync every linked conversation that changed. Returns the reports (empty when idle)."""
        changed = self.changed_keys()
        if not changed:
            return []
        reports: list[SyncReport] = []
        for conversation in self.sync.linked_changed():
            if conversation.claude is None or conversation.claude.key not in changed or conversation.cursor is None:
                continue
            profile = self.sync.profile_for(str(conversation.cursor.ref.source_path))
            busy = bool(profile and cursor_running(profile))
            stamp = (conversation.claude.fingerprint, f"{conversation.cursor.ref.record_count}:{conversation.cursor.ref.updated_ms}", busy)
            if self._last_attempt.get(conversation.key) == stamp and busy:
                continue  # nothing new since the last deferred attempt and Cursor is still open
            self._last_attempt[conversation.key] = stamp
            reports.append(self.sync.sync(conversation, "both", apply=True))
        return reports

    def _emit(self, reports: list[SyncReport]) -> None:
        if not self.on_event or not reports:
            return
        failed = [r for r in reports if r.status == "failed"]
        deferred = [r for r in reports if r.status == "deferred"]
        synced = [r for r in reports if r.status == "synced"]
        if failed:
            self.on_event(AutoSyncEvent("failed", f"{len(failed)} conversation(s) failed to sync: {failed[0].detail}", reports))
        elif deferred:
            self.on_event(AutoSyncEvent("deferred", deferred[0].detail or "waiting", reports))
        elif synced:
            moved = sum(r.to_claude + r.to_cursor for r in synced)
            self.on_event(AutoSyncEvent("synced", f"Synced {len(synced)} conversation(s), {moved} message(s) moved", reports))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._emit(self.run_once())
            except Exception:
                LOG.exception("auto-sync pass failed")
                if self.on_event:
                    self.on_event(AutoSyncEvent("failed", "auto-sync pass failed; see the log"))
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="chatbridge-autosync")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
