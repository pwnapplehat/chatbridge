"""Application service: everything the CLI and the GUI do, with no UI code.

All methods are safe to call from a worker thread (each opens its own read-only database
connection). Cursor data is only ever read; Claude-side files are only created by imports and
only moved (never deleted) by undo.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .catalog import ChatRow, build_rows
from .config import AppPaths, Settings
from .cursor_source import CursorProfile, iter_events, preview_events
from .discovery import discover
from .model import (
    AssistantText,
    ChatRef,
    ConversionReport,
    Counts,
    ImporterError,
    Reasoning,
    ToolCall,
    UserText,
    as_obj,
    as_str,
)
from .osenv import write_text
from .writer import (
    count_written,
    find_sessions_dir,
    folder_aliases,
    import_chat,
    local_session_name,
    session_uuid,
    tally_events,
)

LOG = logging.getLogger(__name__)
PREVIEW_TEXT_LIMIT = 1200


@dataclass(frozen=True)
class ImportRequest:
    """One chat to import, optionally into a user-chosen folder."""

    ref: ChatRef
    cwd_override: str | None = None


@dataclass(frozen=True)
class Progress:
    """Progress notification emitted after each chat finishes."""

    done: int
    total: int
    report: ConversionReport


@dataclass(frozen=True)
class PreviewLine:
    """One line of the read-only chat preview."""

    role: str
    text: str


@dataclass(frozen=True)
class UndoResult:
    """What an undo moved for one chat."""

    chat_id: str
    moved: list[str]
    destination: str


@dataclass(frozen=True)
class VerifyResult:
    """Source-vs-written comparison for one imported chat."""

    chat_id: str
    source: Counts
    written: Counts

    @property
    def exact(self) -> bool:
        return self.source.content_tuple() == self.written.content_tuple()

    @property
    def grown(self) -> bool:
        """The log has extra content (e.g. new messages typed in Claude after the import)."""
        return all(w >= s for w, s in zip(self.written.content_tuple(), self.source.content_tuple(), strict=True))


class ImportService:
    """Facade over discovery, conversion, verification and undo."""

    def __init__(self, paths: AppPaths, settings: Settings) -> None:
        self.paths = paths
        self.settings = settings
        self._aliases: dict[str, str] = {}

    @property
    def aliases(self) -> dict[str, str]:
        """Existing project folders by final name (for mapping old/Windows paths)."""
        return self._aliases

    def profiles(self) -> list[CursorProfile]:
        live = [self.paths.live_profile] if self.paths.live_profile else []
        return [*live, *self.paths.account_profiles, *self.settings.profiles()]

    def load_catalog(self) -> tuple[list[ChatRow], list[str]]:
        """Scan every source. Returns (rows, warnings); a broken source becomes a warning, not a crash."""
        profiles = self.profiles()
        refs, warnings = discover(profiles, self.paths.transcripts_dir)
        folders = [f for p in profiles for f in _safe_workspace_folders(p)]
        self._aliases = folder_aliases(folders)
        return build_rows(refs, self.paths.claude_dir, self._aliases), warnings

    def preview(self, ref: ChatRef, limit: int = 40) -> list[PreviewLine]:
        """First messages of a chat, formatted for display."""
        lines: list[PreviewLine] = []
        for event in preview_events(ref, limit):
            if isinstance(event, UserText):
                lines.append(PreviewLine("You", _clip(event.text)))
            elif isinstance(event, AssistantText):
                lines.append(PreviewLine("Assistant", _clip(event.text)))
            elif isinstance(event, Reasoning):
                lines.append(PreviewLine("Reasoning", _clip(event.text)))
            elif isinstance(event, ToolCall):
                lines.append(PreviewLine("Tool", event.name))
        return lines

    def import_chats(
        self,
        requests: list[ImportRequest],
        apply: bool,
        on_progress: Callable[[Progress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[ConversionReport]:
        """Import chats one by one. A failure in one chat is reported and never stops the others."""
        sessions_dir = find_sessions_dir(self.paths.desktop_dir)
        reports: list[ConversionReport] = []
        for index, request in enumerate(requests, start=1):
            if should_cancel and should_cancel():
                break
            report = self._import_one(request, sessions_dir, apply)
            reports.append(report)
            if on_progress:
                on_progress(Progress(index, len(requests), report))
        return reports

    def _import_one(self, request: ImportRequest, sessions_dir: Path, apply: bool) -> ConversionReport:
        ref = request.ref
        try:
            return import_chat(
                ref, lambda: iter_events(ref), self.paths.claude_dir, sessions_dir, apply, self._aliases, request.cwd_override
            )
        except (ImporterError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            LOG.exception("import failed for chat %s from %s", ref.chat_id, ref.source_path)
            return ConversionReport(ref.chat_id, ref.name or ref.chat_id, ref.source_label, "failed", detail=f"{type(exc).__name__}: {exc}")

    def verify(self, refs: list[ChatRef]) -> list[VerifyResult]:
        """Re-read each imported log and compare it with a fresh read of its source."""
        results: list[VerifyResult] = []
        for ref in refs:
            logs = list((self.paths.claude_dir / "projects").glob(f"*/{session_uuid(ref.chat_id)}.jsonl"))
            if not logs:
                continue
            source, _ = tally_events(iter_events(ref))
            results.append(VerifyResult(ref.chat_id, source, count_written(logs[0])))
        return results

    def undo(self, chat_ids: list[str]) -> list[UndoResult]:
        """Move a chat's imported files into the removed/ folder. Nothing is deleted; files can be moved back."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        results: list[UndoResult] = []
        for chat_id in chat_ids:
            target = self.paths.removed_dir / stamp / chat_id
            moved = self._move_session_files(chat_id, target)
            if moved:
                (target).mkdir(parents=True, exist_ok=True)
                write_text(target / "manifest.json", json.dumps({"chat_id": chat_id, "moved_from": moved}, indent=2))
                results.append(UndoResult(chat_id, moved, str(target)))
        return results

    def _move_session_files(self, chat_id: str, target: Path) -> list[str]:
        """Locate every file belonging to an imported chat and move it under target."""
        projects = self.paths.claude_dir / "projects"
        log_ids = {session_uuid(chat_id)}
        metas = list(self.paths.desktop_dir.glob(f"*/*/{local_session_name(chat_id)}.json"))
        for meta in metas:
            forked = as_str(as_obj(json.loads(meta.read_text(encoding="utf-8"))).get("cliSessionId"))
            if forked:
                log_ids.add(forked)  # the app re-keys a session when it is continued
        sources = [m for m in metas]
        for log_id in sorted(log_ids):
            sources += list(projects.glob(f"*/{log_id}.jsonl")) + list(projects.glob(f"*/{log_id}.jsonl.unverified"))
        moved: list[str] = []
        for source in sources:
            destination = target / source.parent.name / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved.append(str(source))
        return moved


def _safe_workspace_folders(profile: CursorProfile) -> list[str]:
    try:
        return list(profile.workspace_folders().values())
    except OSError:
        return []


def _clip(text: str) -> str:
    return text if len(text) <= PREVIEW_TEXT_LIMIT else text[:PREVIEW_TEXT_LIMIT] + "…"
