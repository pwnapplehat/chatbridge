"""Run blocking work off the Tk main thread and deliver results back on it (Tk is not thread-safe)."""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from typing import TypeVar

LOG = logging.getLogger(__name__)
T = TypeVar("T")


class Bridge:
    """A queue drained by the Tk event loop; worker threads only ever put callables on it."""

    def __init__(self, root: tk.Misc, interval_ms: int = 25) -> None:
        self._root = root
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._interval = interval_ms
        self._closed = False
        self._job: str | None = None
        self._schedule()

    def _schedule(self) -> None:
        if not self._closed:
            self._job = self._root.after(self._interval, self._pump)

    def _pump(self) -> None:
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                job()
            except Exception:
                LOG.exception("UI callback failed")
        try:
            self._schedule()
        except tk.TclError:  # window destroyed
            self._closed = True

    def close(self) -> None:
        self._closed = True
        if self._job is not None:
            with contextlib.suppress(tk.TclError):
                self._root.after_cancel(self._job)

    def call(self, func: Callable[[], None]) -> None:
        """Run `func` on the Tk main thread (safe from any thread)."""
        self._queue.put(func)

    def run_async(self, work: Callable[[], T], on_done: Callable[[T], None], on_error: Callable[[BaseException], None]) -> threading.Thread:
        """Execute `work` in a daemon thread; call on_done / on_error on the main thread."""

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                LOG.exception("background task failed")
                self.call(lambda error=exc: on_error(error))  # type: ignore[misc]
                return
            self.call(lambda: on_done(result))

        thread = threading.Thread(target=runner, daemon=True, name="chatbridge-worker")
        thread.start()
        return thread
