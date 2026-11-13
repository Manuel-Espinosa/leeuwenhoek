"""Leeuwenhoek: embedded debugging panel for Flask."""

from __future__ import annotations

import atexit
import contextvars
import logging
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from flask import Flask, current_app, has_app_context, request

from . import context
from .config import Config
from .context import Batch, BatchKind, current_batch, is_internal
from .filters import EntryFilter, is_ignored
from .models import Entry, jsonable
from .storage import MemoryStorage, SQLAlchemyStorage, StorageDriver
from .tags import Tagger, normalize
from .watchers import AVAILABLE, Watcher, default_watchers
from .watchers.dump import dump
from .watchers.query import QueryWatcher
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
        self._filters: list[EntryFilter] = []
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
            watcher = cls(self, engine) if issubclass(cls, QueryWatcher) else cls(self)
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

    def filter(self, fn: EntryFilter) -> EntryFilter:
        """Register a callback ``fn(entry) -> bool``, ``True`` to keep; usable as a decorator.

        Filters run when a batch is persisted, before tagging. Dropping the root entry drops the
        whole batch. A filter that raises is logged and the entry is kept.
        """
        self._filters.append(fn)
        return fn

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
        if flag is None:
            return self.app is not None and bool(self.app.debug)
        return bool(flag)

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
            path == prefix
            or path.startswith(prefix + "/")
            or is_ignored(path, self.config.ignore_paths)
        )
        _, token = self.open_batch("request", skip=skip)
        request.environ[TOKEN] = token

    def _end(self, exc: BaseException | None) -> None:
        token = request.environ.pop(TOKEN, None)
        batch = current_batch()
        if batch is not None:
            self.close_batch(batch, token, exc)
        else:
            _reset(token)

    def open_batch(
        self, kind: BatchKind, *, parent_id: str | None = None, skip: bool = False
    ) -> tuple[Batch, contextvars.Token]:
        """Make a new batch current; pair it with :meth:`close_batch`, or use :meth:`batch`.

        The batch records unless ``skip`` is true, Leeuwenhoek is disabled, recording is paused
        or the batch is sampled out. One that doesn't record is still current, so watchers can
        check ``batch.recording`` cheaply and nothing inside becomes ``standalone``.
        """
        cfg = self.config
        sampled_out = (
            not cfg.only_errors and cfg.sample_rate < 1 and random.random() >= cfg.sample_rate
        )
        batch = Batch(
            kind=kind,
            parent_id=parent_id,
            recording=not skip and self.enabled() and not sampled_out and self.is_recording(),
            max_entries=cfg.max_entries_per_batch,
        )
        return batch, context.start_batch(batch)

    def close_batch(
        self, batch: Batch, token: contextvars.Token | None, exc: BaseException | None = None
    ) -> None:
        """Run the ``on_finish`` callbacks, persist the batch and restore the previous one."""
        try:
            if batch.recording:
                # Callbacks run while the batch is still current so their entries join it.
                for callback in self.on_finish:
                    try:
                        callback(batch, exc)
                    except Exception:
                        log.exception("A batch finish callback failed")
                if not self.config.only_errors or has_error(batch, exc):
                    self.persist(batch)
        finally:
            _reset(token)

    @contextmanager
    def batch(
        self, kind: BatchKind, *, parent_id: str | None = None, skip: bool = False
    ) -> Iterator[Batch]:
        """Group everything recorded inside the block (a job, a command) in one batch.

        The batch is persisted when the block ends, also when it raises; the exception is passed
        to the ``on_finish`` callbacks and propagates.
        """
        batch, token = self.open_batch(kind, parent_id=parent_id, skip=skip)
        exc: BaseException | None = None
        try:
            yield batch
        except BaseException as e:
            exc = e
            raise
        finally:
            self.close_batch(batch, token, exc)

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
        if batch is None:
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
        if not batch.entries or not self._apply_filters(batch):
            return
        self._apply_tags(batch)
        if self._writer is not None:
            self._writer.put(batch.entries)
        else:
            self._save(batch.entries)

    def _apply_filters(self, batch: Batch) -> bool:
        """Remove the entries a filter rejects. ``False`` if nothing is left to persist."""
        if not self._filters:
            return True
        kept: list[Entry] = []
        for entry in batch.entries:
            if self._keep(entry):
                kept.append(entry)
            elif entry.is_root:
                return False  # without its root entry the batch would be unreachable
        batch.entries = kept
        return bool(kept)

    def _keep(self, entry: Entry) -> bool:
        for fn in self._filters:
            try:
                if not fn(entry):
                    return False
            except Exception:
                log.exception("An entry filter failed")
        return True

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


def _reset(token: contextvars.Token | None) -> None:
    """Restore the batch that was current before ``token`` was set."""
    if token is None:
        return
    try:
        context.end_batch(token)
    except ValueError:  # different Context (async views): clear anyway
        context._current.set(None)


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
