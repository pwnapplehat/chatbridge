"""Everything that differs between Linux, Windows and macOS, in one place.

Other modules ask this one for: where Cursor / Claude / ChatBridge keep their files, how a folder is written as a
``file://`` URI, how Cursor derives a workspace id, whether a Cursor process is running, and how to replace a file
atomically. Nothing here touches a database or a conversation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = not (IS_WINDOWS or IS_MAC)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_URI_DRIVE_RE = re.compile(r"^/[A-Za-z]:")


# --------------------------------------------------------------------------- standard locations
def _env_dir(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def config_root() -> Path:
    """Where applications keep per-user configuration (Cursor's and Claude's data live here too on Linux and Windows)."""
    home = Path.home()
    if IS_WINDOWS:
        return _env_dir("APPDATA", home / "AppData" / "Roaming")
    if IS_MAC:
        return home / "Library" / "Application Support"
    return _env_dir("XDG_CONFIG_HOME", home / ".config")


def data_root() -> Path:
    """Where ChatBridge keeps its own state (link database, undo journals)."""
    home = Path.home()
    if IS_WINDOWS:
        return _env_dir("LOCALAPPDATA", home / "AppData" / "Local")
    if IS_MAC:
        return home / "Library" / "Application Support"
    return _env_dir("XDG_DATA_HOME", home / ".local" / "share")


def app_dir_name() -> str:
    """Folder name used for ChatBridge's own config / data (lower-case on Linux, like the existing installs)."""
    return "chatbridge" if IS_LINUX else "ChatBridge"


def claude_desktop_sessions_dir() -> Path:
    """The Claude desktop app's ``claude-code-sessions`` folder.

    The classic installer keeps it under the roaming config folder. The Microsoft Store (MSIX) build of the app is
    redirected into its package folder, so it is used when it is the only one that exists.
    """
    primary = config_root() / "Claude" / "claude-code-sessions"
    if IS_WINDOWS and not primary.exists():
        local = _env_dir("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        for package in sorted((local / "Packages").glob("Claude_*")) + sorted((local / "Packages").glob("*Claude*")):
            candidate = package / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions"
            if candidate.exists():
                return candidate
    return primary


def cursor_default_data_dir() -> Path:
    """Cursor's default user-data directory (the one used when Cursor starts without --user-data-dir)."""
    return config_root() / "Cursor"


def cursor_accounts_dir() -> Path:
    """Root of extra Cursor 'account' profiles (each started with --user-data-dir=<root>/<name>)."""
    override = os.environ.get("CHATBRIDGE_CURSOR_ACCOUNTS")
    return Path(override) if override else Path.home() / ".cursor-accounts"


# --------------------------------------------------------------------------- paths and URIs
def norm_path(path: str | os.PathLike[str]) -> str:
    """Canonical text for comparing two paths (case-insensitive on Windows, separators unified, no trailing slash)."""
    text = os.path.normpath(os.fspath(path))
    return os.path.normcase(text)


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return norm_path(a) == norm_path(b)


def display_path(path: str) -> str:
    """A path the way Windows tools write it: upper-case drive letter, backslashes. Other systems: unchanged."""
    if IS_WINDOWS and _DRIVE_RE.match(path):
        return path[0].upper() + path[1:].replace("/", "\\")
    return path


def vscode_fs_path(path: str) -> str:
    """``fsPath`` as VS Code / Cursor store it: on Windows a lower-case drive letter and backslashes."""
    if _DRIVE_RE.match(path):
        return path[0].lower() + path[1:].replace("/", "\\")
    return path


def path_to_uri(path: str) -> str:
    """A folder as the ``file://`` URI VS Code / Cursor store (Windows: ``file:///c%3A/Users/me/proj``)."""
    if _DRIVE_RE.match(path):
        rest = path[2:].replace("\\", "/")
        return "file:///" + path[0].lower() + "%3A" + quote(rest, safe="/")
    if path.startswith("\\\\"):  # UNC share
        host, _, tail = path[2:].partition("\\")
        return f"file://{host}/" + quote(tail.replace("\\", "/"), safe="/")
    return "file://" + quote(path, safe="/")


def uri_to_path(uri: str) -> str:
    """Convert a ``file://`` URI into a filesystem path (plain paths are returned unchanged)."""
    if not uri.startswith("file://"):
        return uri
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if _URI_DRIVE_RE.match(path):  # /c:/Users/me -> c:\Users\me (Windows) or c:/Users/me
        path = path[1:]
        return path.replace("/", "\\") if IS_WINDOWS else path
    if parsed.netloc and parsed.netloc != "localhost":  # UNC share
        return f"\\\\{parsed.netloc}{path}".replace("/", "\\") if IS_WINDOWS else f"//{parsed.netloc}{path}"
    return path


def uri_dict(folder: str) -> dict[str, object]:
    """The URI object Cursor stores in ``workspaceIdentifier.uri``."""
    if _DRIVE_RE.match(folder):
        fs = vscode_fs_path(folder)
        return {
            "$mid": 1,
            "fsPath": fs,
            "external": path_to_uri(folder),
            "path": "/" + fs[0] + ":" + fs[2:].replace("\\", "/"),
            "scheme": "file",
        }
    return {"$mid": 1, "fsPath": folder, "external": path_to_uri(folder), "path": folder, "scheme": "file"}


def sqlite_uri(db_path: str | os.PathLike[str], query: str = "mode=ro") -> str:
    """A SQLite ``file:`` URI for any path (handles Windows drive letters, spaces, ``#``, ``?`` and ``%``)."""
    return Path(os.path.abspath(db_path)).as_uri() + ("?" + query if query else "")


def workspace_hash(folder: str) -> str | None:
    """The workspace id Cursor/VS Code assigns to a folder the first time it is opened (None if the folder is gone).

    Linux: md5(path + inode). Windows and macOS: md5(path + creation time in ms); on Windows the path is the VS Code
    ``fsPath`` (lower-case drive letter, backslashes).
    """
    try:
        stat = os.stat(folder)
    except OSError:
        return None
    if IS_LINUX:
        return hashlib.md5(f"{folder}{stat.st_ino}".encode()).hexdigest()
    birth_ns = getattr(stat, "st_birthtime_ns", None)
    if birth_ns is None:
        birth_ns = getattr(stat, "st_birthtime", 0) * 1_000_000_000 if IS_MAC else stat.st_ctime_ns  # Windows < 3.12: ctime is creation
    key = vscode_fs_path(folder) if IS_WINDOWS else folder
    return hashlib.md5(f"{key}{int(birth_ns) // 1_000_000}".encode()).hexdigest()


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc_moment(ts_ms: int) -> datetime:
    """An epoch-millisecond timestamp as an aware UTC datetime (``fromtimestamp`` fails for negative values on Windows)."""
    return _EPOCH + timedelta(milliseconds=ts_ms)


def local_moment(ts_ms: int) -> datetime:
    """An epoch-millisecond timestamp as an aware local datetime (Windows cannot convert times before 1970: fall back to UTC)."""
    moment = utc_moment(ts_ms)
    try:
        return moment.astimezone()
    except (OSError, OverflowError, ValueError):
        return moment


# --------------------------------------------------------------------------- files
def replace_file(source: Path, target: Path, attempts: int = 8) -> None:
    """``os.replace`` that tolerates Windows briefly locking the target (antivirus, indexer, a reader)."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with Unix newlines on every platform (JSON / JSONL must not become CRLF on Windows)."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def append_text(path: Path, text: str) -> None:
    """Append UTF-8 text with Unix newlines, flushed to disk."""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- running Cursor
def _user_data_dir_arg(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg.startswith("--user-data-dir="):
            return arg.split("=", 1)[1].strip('"')
        if arg == "--user-data-dir" and index + 1 < len(args):
            return args[index + 1].strip('"')
    return None


def _is_main_cursor(args: list[str]) -> bool:
    """A Cursor *main* process: the executable is Cursor and it is not an Electron helper (--type=renderer, gpu, ...)."""
    if not args or any(a.startswith("--type=") for a in args):
        return False
    exe = args[0].replace("\\", "/")
    name = exe.rsplit("/", 1)[-1].lower()
    return name in ("cursor", "cursor.exe") or exe.lower().endswith("/cursor/cursor")


def _linux_command_lines() -> list[list[str]]:
    found: list[list[str]] = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            raw = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        found.append([a.decode("utf-8", "replace") for a in raw if a])
    return found


def _psutil_command_lines() -> list[list[str]] | None:
    try:
        import psutil
    except ImportError:
        return None
    found: list[list[str]] = []
    for proc in psutil.process_iter(["name"]):
        try:
            if "cursor" not in (proc.info.get("name") or "").lower():
                continue
            found.append(proc.cmdline())
        except (psutil.Error, OSError):
            continue
    return found


def _powershell_command_lines() -> list[list[str]]:
    """Windows without psutil: ask WMI (slower, roughly half a second)."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='Cursor.exe'\" | Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=flags,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if not out:
        return []
    try:
        lines = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [_split_windows_command(line) for line in ([lines] if isinstance(lines, str) else lines)]


def _split_windows_command(line: str) -> list[str]:
    """Split a Windows command line the way CommandLineToArgvW does."""
    import ctypes

    argc = ctypes.c_int(0)
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined,unused-ignore]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = shell32.CommandLineToArgvW(line, ctypes.byref(argc))
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)  # type: ignore[attr-defined,unused-ignore]


def process_command_lines() -> list[list[str]]:
    """Command lines of candidate Cursor processes (the seam tests replace)."""
    if IS_LINUX:
        return _linux_command_lines()
    via_psutil = _psutil_command_lines()
    if via_psutil is not None:
        return via_psutil
    if IS_WINDOWS:
        return _powershell_command_lines()
    return []


def running_cursor_data_dirs() -> list[Path]:
    """User-data directories of all running Cursor main processes (the default one when no --user-data-dir is given)."""
    found: list[Path] = []
    for args in process_command_lines():
        if not _is_main_cursor(args):
            continue
        explicit = _user_data_dir_arg(args)
        found.append(Path(explicit) if explicit else cursor_default_data_dir())
    return found


def resolved(path: Path) -> str:
    """Comparison key for a directory that may not exist."""
    with contextlib.suppress(OSError):
        return norm_path(path.resolve())
    return norm_path(path)
