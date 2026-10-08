"""Render the main window to a PNG against the real Cursor/Claude data (read-only; throwaway config/data dirs).

Usage: xvfb-run -a python scripts/screenshot.py out.png [--select <title-substring>] [--page activity] [--size 1280x800]
"""

from __future__ import annotations

import argparse
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

from chatbridge.config import AppPaths, Settings  # noqa: E402
from chatbridge.gui.app import ImporterApp  # noqa: E402
from chatbridge.gui.window import MainWindow  # noqa: E402
from chatbridge.sync import SyncService  # noqa: E402


def pump(seconds: float) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--select", default="")
    parser.add_argument("--page", default="conversations")
    parser.add_argument("--wait", type=float, default=10.0)
    parser.add_argument("--demo", action="store_true", help="use a synthetic demo environment instead of the real data")
    parser.add_argument("--activity-demo", action="store_true", help="demo only: run two syncs first so the Activity page has content")
    parser.add_argument("--compare", action="store_true", help="click Compare on the selected conversation first")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        if args.demo:
            from scripts.make_demo import build_demo

            paths, settings = build_demo(Path(tmp) / "demo")
        else:
            base = AppPaths.default()
            paths = replace(base, config_dir=Path(tmp) / "config", data_dir=Path(tmp) / "data")
            settings = Settings()
        app = ImporterApp(application_id="io.github.pwnapplehat.ChatBridgeShot", non_unique=True)
        app.register(None)
        win = MainWindow(app, SyncService(paths, settings), paths, settings)
        win.present()
        pump(args.wait)
        if args.select:
            for index, item in enumerate(win.shown_items()):
                if args.select.lower() in item.conv.title.lower():
                    win.selection.set_selected(index)
                    break
            pump(3)
        if args.activity_demo:
            for conv in win.sync_service.load_conversations()[0]:
                if conv.title in ("Add dark mode toggle", "Migrate users table to uuid"):
                    report = win.sync_service.sync(conv, apply=True)
                    win.activity.add_event(
                        "synced", f"Auto-sync: {conv.title}: {report.to_claude} message(s) to Claude, {report.to_cursor} to Cursor"
                    )
            win.activity.add_event("info", "Auto-sync is on (checks every 20s)")
            win.refresh_journals()
        if args.compare:
            win.detail.compare_button.emit("clicked")
            pump(3)
        win.stack.set_visible_child_name(args.page)
        pump(1)
        paintable = Gtk.WidgetPaintable(widget=win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, win.get_width(), win.get_height())
        rect = Graphene.Rect().init(0, 0, win.get_width(), win.get_height())
        texture = win.get_native().get_renderer().render_texture(snap.to_node(), rect)
        texture.save_to_png(str(args.out))
        print(
            "saved",
            args.out,
            f"{win.get_width()}x{win.get_height()}",
            "items:",
            win.store.get_n_items(),
            "shown:",
            win.sort_model.get_n_items(),
        )


if __name__ == "__main__":
    main()
