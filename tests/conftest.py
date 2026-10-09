"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from chatbridge.config import Settings
from chatbridge.service import ImportService
from tests.fixtures import World, build_world

# GUI test modules import their toolkit at module level, so they are not even collected where it is missing
# (Tk is optional on Linux distributions; GTK is not available on Windows).
collect_ignore: list[str] = []
try:
    import tkinter  # noqa: F401
except ImportError:
    collect_ignore.append("test_tk_gui_e2e.py")
try:
    import gi  # noqa: F401
except ImportError:
    collect_ignore.append("test_gui_e2e.py")


def pytest_configure(config: pytest.Config) -> None:
    """Windows limits paths to 260 characters and Claude's project folder name repeats the whole project path, so keep test paths short."""
    if sys.platform == "win32" and config.option.basetemp is None:
        for root in (Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\"), Path(tempfile.gettempdir())):
            candidate = root / "cbt"
            try:
                candidate.mkdir(exist_ok=True)
            except OSError:
                continue
            config.option.basetemp = candidate
            return


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path)


@pytest.fixture
def service(world: World) -> ImportService:
    return ImportService(world.paths, Settings(extra_profiles=[("backup", str(world.backup_user))]))
