"""Toolkit-neutral pieces shared by the GTK and Tk front ends: state wording, report text, date formatting."""

from __future__ import annotations

from pathlib import Path

from .osenv import local_moment
from .sync import SyncReport, SyncState

STATE_LABELS = {
    SyncState.IN_SYNC: "In sync",
    SyncState.CURSOR_CHANGED: "Cursor is ahead",
    SyncState.CLAUDE_CHANGED: "Claude is ahead",
    SyncState.BOTH_CHANGED: "Both changed",
    SyncState.CURSOR_ONLY: "Only in Cursor",
    SyncState.CLAUDE_ONLY: "Only in Claude",
    SyncState.UNCHECKED: "Not compared yet",
    SyncState.BROKEN: "Counterpart missing",
}
STATE_HELP = {
    SyncState.IN_SYNC: "Both tools have the same messages.",
    SyncState.CURSOR_CHANGED: "Cursor has messages that Claude does not. Sync will add them to the Claude session.",
    SyncState.CLAUDE_CHANGED: "Claude has messages that Cursor does not. Sync will add them to the Cursor chat.",
    SyncState.BOTH_CHANGED: "Both tools have new messages. Sync adds each side's new messages to the other; nothing is overwritten.",
    SyncState.CURSOR_ONLY: "This chat exists only in Cursor. Import it to continue the conversation in Claude.",
    SyncState.CLAUDE_ONLY: "This session exists only in Claude. Send it to Cursor to continue the conversation there.",
    SyncState.UNCHECKED: "These two are the same conversation but have not been compared yet. Compare to see what is missing.",
    SyncState.BROKEN: "One side of this link no longer exists. Stop syncing to forget the link.",
}


def format_when(ms: int) -> str:
    """Short local date/time for a list subtitle."""
    return local_moment(ms).strftime("%b %d, %H:%M") if ms else "unknown date"


def describe(report: SyncReport) -> str:
    """One sentence on what a compare/sync found or will do."""
    if report.status == "failed":
        return f"Could not compare: {report.detail}"
    if report.to_claude == 0 and report.to_cursor == 0 and not report.fixes:
        return "Nothing to sync: both sides already have the same messages."
    parts = []
    if report.to_claude:
        parts.append(f"{report.to_claude} message(s) will be added to Claude")
    if report.to_cursor:
        parts.append(f"{report.to_cursor} message(s) will be added to Cursor")
    parts += [fix[0].upper() + fix[1:] for fix in report.fixes]
    return " and ".join(parts) + "."


def summarize(reports: list[SyncReport], skipped: int = 0) -> str:
    """One-line summary like '2 synced, 1 deferred'."""
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.status] = counts.get(r.status, 0) + 1
    text = ", ".join(f"{n} {status}" for status, n in counts.items()) or "nothing to do"
    return text + (f", {skipped} not started (cancelled)" if skipped else "")


def report_detail(report: SyncReport) -> str:
    detail = f"Claude +{report.to_claude} · Cursor +{report.to_cursor}"
    return detail + (f"\n{report.detail}" if report.detail else "")


def find_user_dir(chosen: Path) -> Path | None:
    """Find the Cursor 'User' folder (the one holding globalStorage/state.vscdb) from any folder in or around it.

    Accepts the user-data folder, its 'User' folder, the folder above it, or any folder inside it
    (for example globalStorage, or a workspaceStorage subfolder).
    """
    candidates = [chosen, chosen / "User", chosen / "data" / "User", chosen / "User" / "User"]
    candidates += list(chosen.parents)[:4]
    candidates += [parent / "User" for parent in list(chosen.parents)[:3]]
    for candidate in candidates:
        if (candidate / "globalStorage" / "state.vscdb").is_file():
            return candidate
    return None
