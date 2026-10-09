"""Running-Cursor detection decides whether a write is safe, so it is tested against real processes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from chatbridge import osenv
from chatbridge.cursor_source import CursorProfile
from chatbridge.cursor_writer import cursor_running


@contextmanager
def fake_cursor(tmp_path: Path, *args: str) -> Iterator[subprocess.Popen[bytes]]:
    """A real process that looks like the Cursor app to the OS: Cursor.exe on Windows, .../cursor/cursor on Linux."""
    base = Path(getattr(sys, "_base_executable", sys.executable))
    env = dict(os.environ)
    if osenv.IS_WINDOWS:
        exe = tmp_path / "bin" / "Cursor.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        if not exe.exists():
            shutil.copy2(base, exe)
        env["PATH"] = str(base.parent) + os.pathsep + env.get("PATH", "")  # the copy finds python3xx.dll next to the original
    else:
        exe = tmp_path / "bin" / "cursor" / "cursor"
        exe.parent.mkdir(parents=True, exist_ok=True)
        if not exe.exists():
            exe.symlink_to(base)
    proc = subprocess.Popen([str(exe), "-c", "import time; time.sleep(60)", *args], env=env)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # wait until the OS lists the process with its arguments
        if any(any(a.startswith("--user-data-dir=") for a in line) for line in osenv.process_command_lines()):
            break
        time.sleep(0.1)
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
    time.sleep(0.3)
    assert cursor_running(profile(mine)) is False, "stops being 'running' once the process exits"


def test_helper_processes_do_not_count(tmp_path: Path) -> None:
    root = tmp_path / "acct-c"
    with fake_cursor(tmp_path, f"--user-data-dir={root}", "--type=renderer"):
        assert cursor_running(profile(root)) is False


# --- the parsing rules, on command lines captured from real Cursor processes ---------------------------------
WINDOWS_MAIN = [r"C:\Users\me\AppData\Local\Programs\cursor\Cursor.exe", r"--user-data-dir=F:\CursorUsers\Aarav\data"]
WINDOWS_HELPER = [
    r"C:\Users\me\AppData\Local\Programs\cursor\Cursor.exe",
    "--type=gpu-process",
    r"--user-data-dir=F:\CursorUsers\Aarav\data",
]
LINUX_MAIN = ["/usr/share/cursor/cursor", "--user-data-dir=/home/me/.cursor-accounts/work"]


def test_command_line_rules() -> None:
    assert osenv._is_main_cursor(WINDOWS_MAIN) and osenv._is_main_cursor(LINUX_MAIN)
    assert not osenv._is_main_cursor(WINDOWS_HELPER)
    assert not osenv._is_main_cursor([r"C:\Windows\explorer.exe"]) and not osenv._is_main_cursor([])
    assert osenv._user_data_dir_arg(WINDOWS_MAIN) == r"F:\CursorUsers\Aarav\data"
    assert osenv._user_data_dir_arg([r"C:\x\Cursor.exe", "--user-data-dir", r"D:\my data"]) == r"D:\my data"
    assert osenv._user_data_dir_arg([r"C:\x\Cursor.exe", r'--user-data-dir="D:\my data"']) == r"D:\my data"
    assert osenv._user_data_dir_arg([r"C:\x\Cursor.exe"]) is None


def test_default_profile_is_used_when_no_data_dir_is_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    default = tmp_path / "Cursor"
    monkeypatch.setattr(osenv, "cursor_default_data_dir", lambda: default)
    monkeypatch.setattr(osenv, "process_command_lines", lambda: [[str(tmp_path / "Cursor.exe")], WINDOWS_HELPER])
    assert osenv.running_cursor_data_dirs() == [default]
    assert cursor_running(profile(default)) is True
    assert cursor_running(profile(tmp_path / "elsewhere")) is False


@pytest.mark.skipif(not osenv.IS_WINDOWS, reason="Windows command-line splitting")
def test_windows_command_line_splitting() -> None:
    line = r'"C:\Program Files\Cursor\Cursor.exe" --user-data-dir="F:\Cursor Users\a b\data" --flag'
    assert osenv._split_windows_command(line) == [
        r"C:\Program Files\Cursor\Cursor.exe",
        r"--user-data-dir=F:\Cursor Users\a b\data",
        "--flag",
    ]
