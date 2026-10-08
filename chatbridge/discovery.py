"""Find every Cursor chat across profiles and transcripts, and pick the richest copy of each."""

from __future__ import annotations

from pathlib import Path

from .cursor_source import CursorProfile, folder_slug_index, list_db_chats, list_transcript_chats
from .model import ChatRef, SourceError

LIVE_PROFILE = CursorProfile(Path.home() / ".config" / "Cursor" / "User", "live")
TRANSCRIPTS_DIR = Path.home() / ".cursor" / "projects"


def parse_profile_arg(arg: str) -> CursorProfile:
    """Parse '<user_dir>' or '<label>=<user_dir>' from the command line."""
    label, sep, path = arg.partition("=")
    return CursorProfile(Path(path if sep else arg), label if sep else Path(arg).parent.name or "profile")


def discover(profiles: list[CursorProfile], transcripts_dir: Path | None) -> tuple[list[ChatRef], list[str]]:
    """Return (best copy of every chat, warnings). A chat present in several sources keeps the one with most records."""
    best: dict[str, ChatRef] = {}
    warnings: list[str] = []
    folders: list[str] = []
    for profile in profiles:
        try:
            chats = list_db_chats(profile)
        except SourceError as exc:
            warnings.append(f"{profile.label}: {exc}")
            continue
        folders.extend(profile.workspace_folders().values())
        for chat in chats:
            current = best.get(chat.chat_id)
            if current is None or chat.record_count > current.record_count:
                best[chat.chat_id] = chat
    if transcripts_dir and transcripts_dir.is_dir():
        folders.extend(c.cwd for c in best.values() if c.cwd)
        for chat in list_transcript_chats(transcripts_dir, folder_slug_index(folders)):
            if chat.chat_id not in best:
                best[chat.chat_id] = chat
    return sorted(best.values(), key=lambda c: c.created_ms), warnings
