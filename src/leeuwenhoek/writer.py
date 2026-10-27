"""Background writer: bounded queue, drop-on-full, lazy thread and periodic pruning."""

from __future__ import annotations

import logging
import os
import queue
import random
import threading
import time
from collections.abc import Callable

from .models import Entry

log = logging.getLogger("leeuwenhoek")

WARN_EVERY = 60.0  # seconds between "dropping batches" warnings


class Writer:
    def __init__(
        self,
        save: Callable[[list[Entry]], None],
        maxsize: int = 1000,
        prune: Callable[[], object] | None = None,
        prune_interval: float = 0,
    ) -> None:
        self._save = save
        self._prune = prune
        self._prune_interval = prune_interval
        self._queue: queue.Queue[list[Entry]] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pid = os.getpid()
        self._next_prune = self._schedule_prune()
        self._last_warning = 0.0
        self.dropped = 0

    def put(self, entries: list[Entry]) -> bool:
        """Enqueue without blocking; if the queue is full the batch is dropped."""
        self._ensure_thread()
        try:
            self._queue.put_nowait(entries)
            return True
        except queue.Full:
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_warning > WARN_EVERY:
                self._last_warning = now
                log.warning("Write queue full, dropping batches (%d dropped so far)", self.dropped)
            return False

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until everything queued is written. ``False`` if ``timeout`` expired first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def _ensure_thread(self) -> None:
        # The thread does not survive a fork (gunicorn --preload), so check the pid too.
        if self._thread is not None and self._thread.is_alive() and self._pid == os.getpid():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._pid == os.getpid():
                return
            self._pid = os.getpid()
            self._thread = threading.Thread(
                target=self._run, name="leeuwenhoek-writer", daemon=True
            )
            self._thread.start()

    def _schedule_prune(self) -> float:
        if not self._prune or self._prune_interval <= 0:
            return float("inf")
        # Jitter so several instances do not prune at the same moment.
        return time.monotonic() + self._prune_interval * random.uniform(0.5, 1.0)

    def _run(self) -> None:
        while True:
            timeout = max(self._next_prune - time.monotonic(), 0.1) if self._prune else None
            try:
                entries = self._queue.get(timeout=timeout)
            except queue.Empty:
                entries = None
            if entries is not None:
                try:
                    self._save(entries)
                finally:
                    self._queue.task_done()
            if self._prune and time.monotonic() >= self._next_prune:
                self._next_prune = self._schedule_prune()
                try:
                    self._prune()
                except Exception:
                    log.exception("Automatic prune failed")
