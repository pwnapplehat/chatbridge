"""Environment diagnostics: what ChatBridge can see on this machine (the first thing to run when something looks wrong)."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass

from . import __version__
from .claude_source import list_claude_sessions
from .config import AppPaths, Settings
from .cursor_source import CursorProfile
from .cursor_writer import cursor_running


@dataclass(frozen=True)
class Check:
    """One diagnostic line. level is 'ok', 'warn' or 'error'."""

    level: str
    text: str


def _profile_check(profile: CursorProfile) -> Check:
    db = profile.db_path
    if not db.is_file():
        return Check("warn", f"Cursor profile '{profile.label}': no database at {db}")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        (chats,) = conn.execute("SELECT count(*) FROM cursorDiskKV WHERE key LIKE 'composerData:%'").fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return Check("error", f"Cursor profile '{profile.label}': cannot read {db} ({exc})")
    state = "RUNNING (writes wait until it closes)" if cursor_running(profile) else "closed"
    mode = "read/write" if profile.writable else "read-only backup"
    return Check("ok", f"Cursor profile '{profile.label}' [{mode}]: {chats} chats, {db.stat().st_size / 1e6:.0f} MB, Cursor is {state}")


def run_doctor(paths: AppPaths, settings: Settings) -> list[Check]:
    """Collect environment checks without modifying anything."""
    checks = [Check("ok", f"ChatBridge {__version__}")]
    profiles = [p for p in [paths.live_profile, *paths.account_profiles, *settings.profiles()] if p is not None]
    checks += [_profile_check(p) for p in profiles] or [
        Check("error", "no Cursor profile found (is Cursor installed and used at least once?)")
    ]
    cursor_bin = shutil.which("cursor")
    checks.append(Check("ok" if cursor_bin else "warn", f"Cursor executable: {cursor_bin or 'not on PATH (not required)'}"))
    if paths.claude_dir.is_dir():
        sessions = list_claude_sessions(paths)
        checks.append(Check("ok", f"Claude data: {paths.claude_dir} ({len(sessions)} sessions with messages)"))
    else:
        checks.append(Check("warn", f"Claude data folder not found: {paths.claude_dir} (open Claude Code / the Claude app once)"))
    desktop = list(paths.desktop_dir.glob("*/*/local_*.json"))
    if desktop:
        checks.append(Check("ok", f"Claude desktop app sessions folder: {paths.desktop_dir} ({len(desktop)} sidebar records)"))
    else:
        checks.append(
            Check(
                "warn", "Claude desktop app sessions folder not found: chats imported into Claude will be written as Claude Code logs only"
            )
        )
    links = paths.links_db
    checks.append(Check("ok", f"Link database: {links} ({'exists' if links.exists() else 'created on first sync'})"))
    checks.append(
        Check(
            "ok",
            f"Undo journals: {paths.journal_dir} ({len(list(paths.journal_dir.glob('*.json'))) if paths.journal_dir.is_dir() else 0} entries)",
        )
    )
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        checks.append(
            Check(
                "ok",
                f"GUI toolkit: GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}",
            )
        )
    except (ImportError, ValueError):
        checks.append(
            Check("warn", "GUI toolkit missing (CLI still works). On Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1")
        )
    return checks
