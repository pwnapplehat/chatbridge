"""The browsable list of Cursor chats: display fields, import status, and filtering (no GUI code)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .model import ChatRef
from .writer import cwd_slug, resolve_cwd, session_uuid


class StatusFilter(Enum):
    ALL = "all"
    NOT_IMPORTED = "not-imported"
    IMPORTED = "imported"


@dataclass(frozen=True)
class ChatFilter:
    """Criteria applied to the catalog. Defaults hide empty drafts and subagent runs."""

    query: str = ""
    project: str | None = None
    status: StatusFilter = StatusFilter.ALL
    include_subagents: bool = False
    include_empty: bool = False
    date_from_ms: int | None = None
    date_to_ms: int | None = None


@dataclass(frozen=True)
class ChatRow:
    """One catalog entry: the source chat plus everything the UI shows about it."""

    ref: ChatRef
    imported: bool
    target_cwd: str

    @property
    def title(self) -> str:
        return self.ref.name.strip() or self.ref.preview.strip() or "(untitled chat)"

    @property
    def project(self) -> str:
        return project_label(self.ref.cwd)

    @property
    def is_empty(self) -> bool:
        return self.ref.record_count == 0


def project_label(cwd: str | None) -> str:
    """Short project name from a (Linux or Windows) path."""
    if not cwd:
        return "(no project)"
    parts = [p for p in re.split(r"[\\/]+", cwd) if p and not p.endswith(":")]
    return parts[-1] if parts else "(no project)"


def is_imported(chat_id: str, claude_dir: Path) -> bool:
    """True when a Claude log for this chat exists in any project folder."""
    return any((claude_dir / "projects").glob(f"*/{session_uuid(chat_id)}.jsonl"))


def build_rows(refs: list[ChatRef], claude_dir: Path, aliases: dict[str, str]) -> list[ChatRow]:
    """Decorate chats with import status and the folder an import would use."""
    return [ChatRow(ref, is_imported(ref.chat_id, claude_dir), resolve_cwd(ref.cwd, aliases)) for ref in refs]


def projects_of(rows: list[ChatRow]) -> list[str]:
    """Sorted distinct project labels present in the catalog."""
    return sorted({row.project for row in rows}, key=str.lower)


def matches(row: ChatRow, criteria: ChatFilter) -> bool:
    """Whether a row passes every active criterion."""
    if row.is_empty and not criteria.include_empty:
        return False
    if row.ref.is_subagent and not criteria.include_subagents:
        return False
    if criteria.status is StatusFilter.IMPORTED and not row.imported:
        return False
    if criteria.status is StatusFilter.NOT_IMPORTED and row.imported:
        return False
    if criteria.project is not None and row.project != criteria.project:
        return False
    if criteria.date_from_ms is not None and row.ref.created_ms < criteria.date_from_ms:
        return False
    if criteria.date_to_ms is not None and row.ref.created_ms > criteria.date_to_ms:
        return False
    needle = criteria.query.strip().lower()
    if needle:
        haystack = " ".join((row.title, row.ref.preview, row.project, row.ref.cwd or "", row.ref.chat_id, row.ref.source_label)).lower()
        return all(word in haystack for word in needle.split())
    return True


def apply_filter(rows: list[ChatRow], criteria: ChatFilter) -> list[ChatRow]:
    """Filter rows and sort newest first."""
    return sorted((r for r in rows if matches(r, criteria)), key=lambda r: r.ref.created_ms, reverse=True)


def target_slug(row: ChatRow) -> str:
    """Claude project-folder slug an import of this row would use."""
    return cwd_slug(row.target_cwd)
