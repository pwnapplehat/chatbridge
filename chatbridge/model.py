"""Shared data model and small typed JSON helpers for the Cursor -> Claude importer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

JsonObj = dict[str, object]


class ImporterError(Exception):
    """Base class for every error this package raises deliberately."""


class SourceError(ImporterError):
    """A Cursor data source could not be read or is structurally invalid."""


class WriteError(ImporterError):
    """The Claude-side files could not be written."""


def as_obj(value: object) -> JsonObj:
    """Return value if it is a JSON object, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """Return value if it is a JSON array, otherwise an empty list."""
    return value if isinstance(value, list) else []


def as_str(value: object, default: str = "") -> str:
    """Return value if it is a string, otherwise default."""
    return value if isinstance(value, str) else default


def as_int(value: object, default: int = 0) -> int:
    """Return value if it is an int (not bool), otherwise default."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


@dataclass(frozen=True)
class UserText:
    """A message the human typed in Cursor."""

    text: str
    ts_ms: int
    image_count: int = 0


@dataclass(frozen=True)
class AssistantText:
    """Visible assistant prose."""

    text: str
    ts_ms: int


@dataclass(frozen=True)
class Reasoning:
    """Assistant reasoning/thinking text (kept as text marked 'Cursor reasoning')."""

    text: str
    ts_ms: int


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation with its stored output (output is None if Cursor never stored one)."""

    seq: int
    call_id: str
    name: str
    tool_input: JsonObj
    output: str | None
    is_error: bool
    ts_ms: int


@dataclass(frozen=True)
class EmptyRecord:
    """A stored record that carries no user-visible content (accounted for, never silently lost)."""


Event = UserText | AssistantText | Reasoning | ToolCall | EmptyRecord
SourceKind = Literal["db", "transcript"]


@dataclass(frozen=True)
class Counts:
    """Content tallies used to prove that nothing was dropped during conversion."""

    user: int = 0
    assistant_text: int = 0
    reasoning: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    text_chars: int = 0
    empty_records: int = 0

    def content_tuple(self) -> tuple[int, int, int, int, int, int]:
        """The fields that must match between source and written output."""
        return (self.user, self.assistant_text, self.reasoning, self.tool_calls, self.tool_results, self.text_chars)


@dataclass(frozen=True)
class ChatRef:
    """A chat discovered in one source, before conversion."""

    chat_id: str
    name: str
    created_ms: int
    cwd: str | None
    is_subagent: bool
    kind: SourceKind
    source_path: Path
    source_label: str
    record_count: int
    expected_visible: int = 0
    preview: str = ""
    updated_ms: int = 0


@dataclass
class ConversionReport:
    """Outcome of importing one chat."""

    chat_id: str
    title: str
    source_label: str
    status: Literal["written", "exists", "empty", "dry-run", "failed"]
    cwd: str = ""
    log_path: str = ""
    source_counts: Counts = field(default_factory=Counts)
    written_counts: Counts | None = None
    log_bytes: int = 0
    detail: str = ""

    @property
    def verified(self) -> bool:
        """True when the written log carries exactly the source's content counts."""
        return self.written_counts is not None and (self.written_counts.content_tuple() == self.source_counts.content_tuple())
