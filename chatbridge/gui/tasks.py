"""Run blocking work off the GTK main thread and deliver results back on it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TypeVar

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

LOG = logging.getLogger(__name__)
T = TypeVar("T")


def run_async(work: Callable[[], T], on_done: Callable[[T], None], on_error: Callable[[BaseException], None]) -> threading.Thread:
    """Execute `work` in a daemon thread; call on_done/on_error on the main loop."""

    def runner() -> None:
        try:
            result = work()
        except Exception as exc:
            LOG.exception("background task failed")
            GLib.idle_add(_deliver_error, on_error, exc)
            return
        GLib.idle_add(_deliver, on_done, result)

    thread = threading.Thread(target=runner, daemon=True, name="chatbridge-worker")
    thread.start()
    return thread


def call_on_main(func: Callable[[], None]) -> None:
    """Schedule `func` on the GTK main loop (safe to call from any thread)."""
    GLib.idle_add(_invoke, func)


def _deliver(callback: Callable[[T], None], result: T) -> bool:
    callback(result)
    return False


def _deliver_error(callback: Callable[[BaseException], None], exc: BaseException) -> bool:
    callback(exc)
    return False


def _invoke(func: Callable[[], None]) -> bool:
    func()
    return False
