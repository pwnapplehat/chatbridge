"""Application entry point for the GTK4 front end."""

from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from ..config import AppPaths, Settings, SettingsError, load_settings  # noqa: E402
from ..sync import SyncService  # noqa: E402
from .window import MainWindow  # noqa: E402

APP_ID = "io.github.pwnapplehat.ChatBridge"


class ImporterApp(Adw.Application):
    """Creates the main window; paths are injectable so tests never touch real data."""

    def __init__(
        self,
        paths: AppPaths | None = None,
        settings: Settings | None = None,
        application_id: str = APP_ID,
        non_unique: bool = False,
    ) -> None:
        flags = Gio.ApplicationFlags.NON_UNIQUE if non_unique else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=application_id, flags=flags)
        self.paths = paths or AppPaths.default()
        self._settings = settings
        self.window: MainWindow | None = None
        self.connect("activate", self._on_activate)

    def _on_activate(self, _app: Adw.Application) -> None:
        if self.window is not None:
            self.window.present()
            return
        try:
            settings = self._settings or load_settings(self.paths)
        except SettingsError:
            logging.getLogger(__name__).exception("settings unreadable; using defaults")
            settings = Settings()
        self.window = MainWindow(self, SyncService(self.paths, settings), self.paths, settings)
        self.window.present()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return int(ImporterApp().run(argv))


def main_entry() -> None:
    """Console-script entry point."""
    sys.exit(main(sys.argv))
