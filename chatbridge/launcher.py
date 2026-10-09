"""`chatbridge-gui`: the GTK 4 / libadwaita app where it is available (Linux), the Tk app everywhere else (Windows, macOS)."""

from __future__ import annotations

import sys

from .osenv import IS_LINUX


def gtk_available() -> bool:
    if not IS_LINUX:
        return False
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def main_entry() -> None:
    if gtk_available():
        from .gui.app import main as gtk_main

        sys.exit(gtk_main(sys.argv))
    from .tkgui.app import main as tk_main

    sys.exit(tk_main(sys.argv))
