"""Persistent links between a Cursor chat and a Claude session (SQLite, in the app data folder).

A link records which two conversations are the same, where it came from, when it last synced and cheap
fingerprints of both sides at that moment (so 'changed since last sync' can be answered without reading chats).
Losing the database loses no conversation data: ids are deterministic, so links are re-detected.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

Origin = Literal["cursor", "claude"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cursor_db TEXT NOT NULL,
    cursor_chat_id TEXT NOT NULL,
    claude_key TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    last_sync_ms INTEGER NOT NULL DEFAULT 0,
    cursor_fp TEXT NOT NULL DEFAULT '',
    claude_fp TEXT NOT NULL DEFAULT '',
    auto_sync INTEGER NOT NULL DEFAULT 1,
    UNIQUE (cursor_db, cursor_chat_id),
    UNIQUE (claude_key)
)
"""


@dataclass(frozen=True)
class Link:
    """A paired Cursor chat and Claude session."""

    cursor_db: str
    cursor_chat_id: str
    claude_key: str
    origin: Origin
    created_ms: int
    last_sync_ms: int = 0
    cursor_fp: str = ""
    claude_fp: str = ""
    auto_sync: bool = True


class LinkStore:
    """Thin SQLite wrapper; every call opens its own connection so it is safe from any thread."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open the database; it (and its folder) is only created on the first write, so reads leave no trace."""
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        if write:
            conn.execute(SCHEMA)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Link:
        return Link(
            cursor_db=row["cursor_db"], cursor_chat_id=row["cursor_chat_id"], claude_key=row["claude_key"], origin=row["origin"],
            created_ms=row["created_ms"], last_sync_ms=row["last_sync_ms"], cursor_fp=row["cursor_fp"], claude_fp=row["claude_fp"],
            auto_sync=bool(row["auto_sync"]),
        )  # fmt: skip

    def all(self) -> list[Link]:
        if not self.path.exists():
            return []
        with self._connect() as conn:
            return [self._row(r) for r in conn.execute("SELECT * FROM links ORDER BY created_ms")]

    def by_cursor(self, cursor_db: str, chat_id: str) -> Link | None:
        if not self.path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM links WHERE cursor_db = ? AND cursor_chat_id = ?", (cursor_db, chat_id)).fetchone()
        return self._row(row) if row else None

    def by_claude(self, key: str) -> Link | None:
        if not self.path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM links WHERE claude_key = ?", (key,)).fetchone()
        return self._row(row) if row else None

    def save(self, link: Link) -> None:
        """Insert or replace the link identified by its Claude key."""
        with self._connect(write=True) as conn:
            conn.execute(
                "DELETE FROM links WHERE cursor_db = ? AND cursor_chat_id = ? AND claude_key != ?",
                (link.cursor_db, link.cursor_chat_id, link.claude_key),
            )
            conn.execute(
                "INSERT INTO links (cursor_db, cursor_chat_id, claude_key, origin, created_ms, last_sync_ms, cursor_fp, claude_fp, auto_sync) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(claude_key) DO UPDATE SET cursor_db=excluded.cursor_db, "
                "cursor_chat_id=excluded.cursor_chat_id, origin=excluded.origin, last_sync_ms=excluded.last_sync_ms, "
                "cursor_fp=excluded.cursor_fp, claude_fp=excluded.claude_fp, auto_sync=excluded.auto_sync",
                (link.cursor_db, link.cursor_chat_id, link.claude_key, link.origin, link.created_ms, link.last_sync_ms,
                 link.cursor_fp, link.claude_fp, int(link.auto_sync)),
            )  # fmt: skip

    def record_sync(self, link: Link, cursor_fp: str, claude_fp: str) -> Link:
        """Store fingerprints of both sides right after a successful sync."""
        updated = replace(link, last_sync_ms=int(time.time() * 1000), cursor_fp=cursor_fp, claude_fp=claude_fp)
        self.save(updated)
        return updated

    def set_auto(self, claude_key: str, enabled: bool) -> None:
        with self._connect(write=True) as conn:
            conn.execute("UPDATE links SET auto_sync = ? WHERE claude_key = ?", (int(enabled), claude_key))

    def remove(self, claude_key: str) -> None:
        """Forget a link (conversations on both sides are untouched)."""
        if not self.path.exists():
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM links WHERE claude_key = ?", (claude_key,))
