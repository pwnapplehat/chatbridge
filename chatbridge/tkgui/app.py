"""Application entry point for the Tk front end."""

from __future__ import annotations

import logging
import sys
import tkinter as tk

from ..config import AppPaths, Settings, SettingsError, load_settings
from ..sync import SyncService
from .window import MainWindow, enable_dpi_awareness


def create_window(root: tk.Tk, paths: AppPaths | None = None, settings: Settings | None = None) -> MainWindow:
    """Build the main window on a (hidden) root; paths are injectable so tests never touch real data."""
    paths = paths or AppPaths.default()
    try:
        loaded = settings or load_settings(paths)
    except SettingsError:
        logging.getLogger(__name__).exception("settings unreadable; using defaults")
        loaded = Settings()
    return MainWindow(root, SyncService(paths, loaded), paths, loaded, on_closed=root.destroy)


def _has_console() -> bool:
    """Whether stderr goes somewhere real (windowed launchers have no stderr, or a stand-in without a file descriptor)."""
    try:
        return sys.stderr is not None and sys.stderr.fileno() >= 0
    except (OSError, ValueError, AttributeError):
        return False


def _configure_logging() -> None:
    """Log to the console when there is one; the windowed launcher (pythonw) has none, so log to a file instead."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if _has_console():
        logging.basicConfig(level=logging.INFO, format=fmt)
        return
    log = AppPaths.default().data_dir / "chatbridge-gui.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format=fmt, filename=log, encoding="utf-8")
    except OSError:
        logging.basicConfig(level=logging.CRITICAL)


def main(argv: list[str]) -> int:
    _configure_logging()
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    create_window(root)
    root.mainloop()
    return 0


def main_entry() -> None:
    """Console-script entry point."""
    sys.exit(main(sys.argv))
