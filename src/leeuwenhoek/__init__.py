"""Leeuwenhoek: embedded debugging panel for Flask."""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from flask import Flask, request

from . import context
from .config import Config
from .context import Batch, current_batch, is_internal
from .filters import is_ignored
from .models import Entry, jsonable
from .storage import MemoryStorage, SQLAlchemyStorage, StorageDriver
from .watchers import AVAILABLE, Watcher
from .watchers.dump import dump

__all__ = ["Leeuwenhoek", "dump"]

log = logging.getLogger("leeuwenhoek")

TOKEN = "leeuwenhoek.token"
RECORDING_TTL = 1.0  # seconds the recording flag is cached


class Leeuwenhoek:
    def __init__(self, app: Flask | None = None, **kwargs: Any) -> None:
        self.app: Flask | None = None
        self.config: Config
        self.storage: StorageDriver
        self.watchers: dict[str, Watcher] = {}
        #: callbacks ``fn(batch, exc)`` run when a batch closes, before it is persisted
        self.on_finish: list[Callable[[Batch, BaseException | None], None]] = []
        self._authorize: Callable[[], bool] | None = None
        self._queue: queue.Queue[list[Entry] | None] | None = None
        self._recording = (True, 0.0)
        if app is not None:
            self.init_app(app, **kwargs)

    # ------------------------------------------------------------------ setup

    def init_app(self, app: Flask, engine: Any = None) -> None:
        from .blueprint import bp

        self.app = app
        self.config = Config(app)
        cfg = self.config
        if cfg.storage == "memory":
            self.storage = MemoryStorage()
        else:
            self.storage = SQLAlchemyStorage(cfg.database_uri)
        self.storage.install()
        app.extensions["leeuwenhoek"] = self

        app.before_request(self._begin)
        app.teardown_request(self._end)

        for name in cfg.watchers:
            cls = AVAILABLE.get(name)
            if cls is None:
                log.warning("Watcher desconocido: %s", name)
                continue
            watcher = cls(self, engine) if name == "query" else cls(self)
            watcher.register(app)
            self.watchers[name] = watcher

        app.register_blueprint(bp, url_prefix=cfg.url_prefix)
        csrf = app.extensions.get("csrf")  # Flask-WTF: the panel uses its own token
        if csrf is not None:
            csrf.exempt(bp)

        if cfg.async_write:
            self._queue = queue.Queue()
            threading.Thread(target=self._writer, name="leeuwenhoek-writer", daemon=True).start()
            atexit.register(self.flush)

    def close(self) -> None:
        """Remove global handlers and listeners (useful in tests)."""
        for watcher in self.watchers.values():
            watcher.unregister()
        self.flush()
        self.storage.close()

    def authorize(self, rule: Callable[[], bool]) -> Callable[[], bool]:
        """Panel access rule. Replaces the default check (``app.debug``)."""
        self._authorize = rule
        return rule

    # ------------------------------------------------------------------ state

    def enabled(self) -> bool:
        flag = self.config.enabled
        return bool(self.app.debug) if flag is None else bool(flag)

    def is_recording(self) -> bool:
        value, at = self._recording
        if time.monotonic() - at > RECORDING_TTL:
            with context.internal():
                value = self.storage.get_setting("recording", "1") == "1"
            self._recording = (value, time.monotonic())
        return value

    def set_recording(self, on: bool) -> None:
        with context.internal():
            self.storage.set_setting("recording", "1" if on else "0")
        self._recording = (on, time.monotonic())

    def current(self) -> Batch | None:
        return current_batch()

    # ------------------------------------------------------------------ batches

    def _begin(self) -> None:
        path = request.path
        prefix = self.config.url_prefix.rstrip("/")
        skip = (
            not self.enabled()
            or path == prefix
            or path.startswith(prefix + "/")
            or is_ignored(path, self.config.ignore_paths)
        )
        batch = Batch(
            kind="request",
            recording=not skip and self.is_recording(),
            max_entries=self.config.max_entries_per_batch,
        )
        request.environ[TOKEN] = context.start_batch(batch)

    def _end(self, exc: BaseException | None) -> None:
        token = request.environ.pop(TOKEN, None)
        batch = current_batch()
        try:
            if batch is not None and batch.recording:
                # Callbacks run while the batch is still current so their entries join it.
                for callback in self.on_finish:
                    try:
                        callback(batch, exc)
                    except Exception:
                        log.exception("A batch finish callback failed")
                self.persist(batch)
        finally:
            if token is not None:
                try:
                    context.end_batch(token)
                except ValueError:  # different Context (async views): clear anyway
                    context._current.set(None)

    def record(
        self,
        type: str,
        content: dict[str, Any],
        *,
        tags: list[str] | None = None,
        family_hash: str | None = None,
        is_root: bool = False,
        force: bool = False,
    ) -> Entry | None:
        """Add an entry to the current batch; outside one, store it as ``standalone``."""
        if is_internal() or self.app is None or not self.enabled():
            return None
        batch = current_batch()
        standalone = batch is None
        if standalone:
            if not self.is_recording():
                return None
            batch = Batch(kind="standalone", max_entries=1)
            is_root = True
        if not batch.recording:
            return None
        entry = Entry(
            type=type,
            content=jsonable(content),
            batch_id=batch.id,
            parent_batch_id=batch.parent_id,
            family_hash=family_hash,
            is_root=is_root,
            tags=tags or [],
        )
        if not batch.add(entry, force):
            return None
        if standalone:
            self.persist(batch)
        return entry

    def persist(self, batch: Batch) -> None:
        if not batch.entries:
            return
        if self._queue is not None:
            self._queue.put(batch.entries)
        else:
            self._save(batch.entries)

    def _save(self, entries: list[Entry]) -> None:
        with context.internal():
            try:
                self.storage.save(entries)
            except Exception:
                log.exception("No se pudo guardar el batch")

    def _writer(self) -> None:
        assert self._queue is not None
        while True:
            entries = self._queue.get()
            try:
                if entries:
                    self._save(entries)
            finally:
                self._queue.task_done()

    def flush(self) -> None:
        """Block until all pending batches have been written."""
        if self._queue is not None:
            self._queue.join()

    def prune(self, hours: float | None = None) -> int:
        return self.storage.prune(self.config.prune_hours if hours is None else hours)
