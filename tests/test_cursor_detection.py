"""Running-Cursor detection decides whether a write is safe, so it is tested against real processes."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from chatbridge.cursor_source import CursorProfile
from chatbridge.cursor_writer import cursor_running


@contextmanager
def fake_cursor(tmp_path: Path, *args: str) -> Iterator[subprocess.Popen[bytes]]:
    """A process whose executable path ends in /cursor/cursor (python under that name), like the real app."""
    exe = tmp_path / "bin" / "cursor" / "cursor"
    exe.parent.mkdir(parents=True, exist_ok=True)
    if not exe.exists():
        exe.symlink_to(sys.executable)
    proc = subprocess.Popen([str(exe), "-c", "import time; time.sleep(60)", *args])
    time.sleep(0.4)
    try:
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def profile(root: Path) -> CursorProfile:
    return CursorProfile(root / "User", "p", writable=True)


def test_detects_main_process_for_its_own_profile_only(tmp_path: Path) -> None:
    mine, other = tmp_path / "acct-a", tmp_path / "acct-b"
    with fake_cursor(tmp_path, f"--user-data-dir={mine}"):
        assert cursor_running(profile(mine)) is True
        assert cursor_running(profile(other)) is False
    time.sleep(0.2)
    assert cursor_running(profile(mine)) is False, "stops being 'running' once the process exits"


def test_helper_processes_do_not_count(tmp_path: Path) -> None:
    root = tmp_path / "acct-c"
    with fake_cursor(tmp_path, f"--user-data-dir={root}", "--type=renderer"):
        assert cursor_running(profile(root)) is False
