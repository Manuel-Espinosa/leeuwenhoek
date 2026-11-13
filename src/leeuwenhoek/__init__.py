"""Leeuwenhoek: embedded debugging panel for Flask."""

from __future__ import annotations

import atexit
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from flask import Flask, current_app, has_app_context, request

from . import context
from .config import Config
from .context import Batch, current_batch, is_internal
from .filters import is_ignored
from .models import Entry, jsonable
from .storage import MemoryStorage, SQLAlchemyStorage, StorageDriver
from .tags import Tagger, normalize
from .watchers import AVAILABLE, Watcher, default_watchers
from .watchers.dump import dump
from .writer import Writer

__all__ = ["Leeuwenhoek", "add_tags", "dump"]

log = logging.getLogger("leeuwenhoek")

TOKEN = "leeuwenhoek.token"
RECORDING_TTL = 5.0  # seconds the recording flag is cached (one DB read per instance)


class Leeuwenhoek:
    def __init__(self, app: Flask | None = None, **kwargs: Any) -> None:
        self.app: Flask | None = None
        self.config: Config
        self.storage: StorageDriver
        self.watchers: dict[str, Watcher] = {}
        #: callbacks ``fn(batch, exc)`` run when a batch closes, before it is persisted
        self.on_finish: list[Callable[[Batch, BaseException | None], None]] = []
        self._authorize: Callable[[], bool] | None = None
        self._taggers: list[Tagger] = []
        self._writer: Writer | None = None
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
            self.storage = SQLAlchemyStorage(cfg.database_uri, cfg.engine_options)
        if cfg.auto_install:
            try:
                self.storage.install()
            except Exception:  # several instances may race to create the tables
                log.exception("Could not create Leeuwenhoek tables")
        app.extensions["leeuwenhoek"] = self

        app.before_request(self._begin)
        app.teardown_request(self._end)

        names = default_watchers() if cfg.watchers is None else cfg.watchers
        for name in names:
            cls = AVAILABLE.get(name)
            if cls is None:
                log.warning("Unknown watcher: %s", name)
                continue
            if missing := cls.missing():
                log.warning("Skipping watcher %s: %s not installed", name, ", ".join(missing))
                continue
            watcher = cls(self, engine) if name == "query" else cls(self)
            watcher.register(app)
            self.watchers[name] = watcher

        app.register_blueprint(bp, url_prefix=cfg.url_prefix)
        csrf = app.extensions.get("csrf")  # Flask-WTF: the panel uses its own token
        if csrf is not None:
            csrf.exempt(bp)

        if cfg.async_write:
            interval = cfg.prune_interval_minutes * 60 if cfg.auto_prune else 0
            self._writer = Writer(
                self._save, cfg.queue_max_batches, prune=self.prune, prune_interval=interval
            )
            atexit.register(self.flush, cfg.flush_timeout)

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

    def tag(self, tagger: Tagger) -> Tagger:
        """Register a callback ``fn(entry) -> iterable of tags``; usable as a decorator."""
        self._taggers.append(tagger)
        return tagger

    def add_tags(self, *tags: str) -> bool:
        """Tag the current batch; the tags reach every entry of it. ``False`` if none is active."""
        batch = current_batch()
        if batch is None or not batch.recording:
            return False
        batch.tags.extend(tags)
        return True

    # ------------------------------------------------------------------ state

    def enabled(self) -> bool:
        flag = self.config.enabled
        return bool(self.app.debug) if flag is None else bool(flag)

    def is_recording(self) -> bool:
        """Cached pause flag. Never raises: if the storage is down it keeps the last value."""
        value, at = self._recording
        if time.monotonic() - at > RECORDING_TTL:
            try:
                with context.internal():
                    value = self.storage.get_setting("recording", "1") == "1"
            except Exception:
                log.exception("Could not read the recording flag")
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
        cfg = self.config
        sampled_out = (
            not cfg.only_errors and cfg.sample_rate < 1 and random.random() >= cfg.sample_rate
        )
        batch = Batch(
            kind="request",
            recording=not skip and not sampled_out and self.is_recording(),
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
                if not self.config.only_errors or has_error(batch, exc):
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
            if not self.is_recording() or (self.config.only_errors and type != "exception"):
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
        self._apply_tags(batch)
        if self._writer is not None:
            self._writer.put(batch.entries)
        else:
            self._save(batch.entries)

    def _apply_tags(self, batch: Batch) -> None:
        for entry in batch.entries:
            extra: list[str] = []
            for tagger in self._taggers:
                try:
                    extra.extend(tagger(entry) or ())
                except Exception:
                    log.exception("A tagger failed")
            entry.tags = normalize([*entry.tags, *batch.tags, *extra])

    def _save(self, entries: list[Entry]) -> None:
        with context.internal():
            try:
                self.storage.save(entries)
            except Exception:
                log.exception("Could not save the batch")

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until pending batches are written; ``False`` if ``timeout`` expired first."""
        return self._writer.flush(timeout) if self._writer is not None else True

    def prune(self, hours: float | None = None) -> int:
        return self.storage.prune(self.config.prune_hours if hours is None else hours)


def has_error(batch: Batch, exc: BaseException | None) -> bool:
    """An unhandled exception, a 5xx response or a captured exception entry."""
    status = batch.meta.get("status") or 0
    return exc is not None or status >= 500 or any(e.type == "exception" for e in batch.entries)


def add_tags(*tags: str) -> bool:
    """Tag the current batch from anywhere (views, use cases, workers)."""
    if not has_app_context():
        return False
    lw = current_app.extensions.get("leeuwenhoek")
    return lw.add_tags(*tags) if lw else False
