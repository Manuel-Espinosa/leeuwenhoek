"""Flask-Caching operations (``get``, ``set``, ``delete``, ``clear`` and the ``*_many`` variants)."""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Any

from flask import current_app, has_app_context

from ..filters import MASK, limit_payload, redact
from ..models import jsonable
from .base import Watcher

log = logging.getLogger("leeuwenhoek")

#: ``Cache`` methods that are wrapped, and the kind of operation each one is.
READS = ("get", "get_many", "get_dict", "has")
WRITES = ("set", "add", "set_many")
DELETES = ("delete", "delete_many", "unlink", "clear")
METHODS = READS + WRITES + DELETES

#: True while a wrapped method runs, so the calls it makes to other wrapped methods
#: (``unlink`` -> ``delete_many``) are not recorded a second time.
_inside: ContextVar[bool] = ContextVar("leeuwenhoek.cache_inside", default=False)

# The patch is applied to the ``Cache`` class once, shared by every active watcher, and
# removed when the last one unregisters.
_lock = threading.Lock()
_active: set[CacheWatcher] = set()
_originals: dict[str, Callable[..., Any]] = {}
_target: type | None = None


class CacheWatcher(Watcher):
    name = "cache"
    entry_type = "cache"
    label = "Cache"
    requires = "flask_caching"

    def register(self, app) -> None:
        from flask_caching import Cache

        global _target
        with _lock:
            if not _active:
                _target = Cache
                for method in METHODS:
                    original = Cache.__dict__.get(method)
                    if original is None:  # older Flask-Caching without this proxy
                        continue
                    _originals[method] = original
                    setattr(Cache, method, _wrap(method, original))
            _active.add(self)

    def unregister(self) -> None:
        global _target
        with _lock:
            _active.discard(self)
            if _active or _target is None:
                return
            for method, original in _originals.items():
                setattr(_target, method, original)
            _originals.clear()
            _target = None

    def capture(
        self,
        cache: Any,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result: Any,
        duration_ms: float,
    ) -> None:
        batch = self.lw.current()
        if batch is not None and not batch.recording:
            return
        cfg = self.lw.config
        content: dict[str, Any] = {"operation": method}
        values: dict[str, Any] = {}

        if method in ("get", "has"):
            key = _arg(args, kwargs, 0, "key")
            keys = [key]
            hit = bool(result) if method == "has" else result is not None
            content.update(hit=hit, hits=int(hit), misses=int(not hit))
            if method == "get" and hit:
                values[key] = result
        elif method in ("get_many", "get_dict"):
            keys = list(args)
            found = list(result.values()) if isinstance(result, dict) else list(result or [])
            hits = [k for k, v in zip(keys, found) if v is not None]
            content.update(hits=len(hits), misses=len(keys) - len(hits))
            values.update((k, v) for k, v in zip(keys, found) if v is not None)
        elif method in ("set", "add"):
            key = _arg(args, kwargs, 0, "key")
            keys = [key]
            values[key] = _arg(args, kwargs, 1, "value")
            content["timeout"] = _timeout(cache, _arg(args, kwargs, 2, "timeout"))
        elif method == "set_many":
            mapping = _arg(args, kwargs, 0, "mapping") or {}
            keys = list(mapping)
            values.update(mapping)
            content["timeout"] = _timeout(cache, _arg(args, kwargs, 1, "timeout"))
        elif method == "delete":
            keys = [_arg(args, kwargs, 0, "key")]
        elif method == "clear":
            keys = []
        else:  # delete_many, unlink
            keys = list(args)

        content["keys"] = [str(k) for k in keys]
        if method in ("set", "add", "delete"):
            content["result"] = result if isinstance(result, bool) else None
        if cfg.cache_values and values:
            content["values"] = {
                str(k): self._value(k, v, cfg.hidden_params, cfg.max_body_kb)
                for k, v in values.items()
            }
        content["backend"] = _backend(cache)
        content["duration_ms"] = round(duration_ms, 3)
        self.record(content)

    @staticmethod
    def _value(key: Any, value: Any, hidden: Iterable[str], max_kb: int) -> Any:
        if hidden_key(key, hidden):
            return MASK
        return limit_payload(redact(jsonable(value), hidden), max_kb)


def hidden_key(key: Any, hidden: Iterable[str]) -> bool:
    """Whether a cache key contains one of the hidden names (case-insensitive substring).

    Cache keys are usually namespaced (``user:1:token``), so a plain equality check like the
    one used for body fields would miss them.
    """
    text = str(key).lower()
    return any(name and name.lower() in text for name in hidden)


def _wrap(method: str, original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _inside.get():
            return original(self, *args, **kwargs)
        token = _inside.set(True)
        started = time.perf_counter()
        try:
            result = original(self, *args, **kwargs)
        finally:
            _inside.reset(token)
        duration = (time.perf_counter() - started) * 1000
        try:
            watcher = _watcher_for(self)
            if watcher is not None:
                watcher.capture(self, method, args, kwargs, result, duration)
        except Exception:  # recording must never break the app's cache call
            log.exception("Could not record a cache operation")
        return result

    return wrapper


def _watcher_for(cache: Any) -> CacheWatcher | None:
    """The cache watcher of the Leeuwenhoek instance on the app the cache call runs for."""
    if has_app_context():
        app = current_app._get_current_object()  # type: ignore[attr-defined]
    else:
        app = getattr(cache, "app", None)
    lw = app.extensions.get("leeuwenhoek") if app is not None else None
    watcher = lw.watchers.get("cache") if lw is not None else None
    return watcher if watcher in _active else None


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], index: int, name: str) -> Any:
    if len(args) > index:
        return args[index]
    return kwargs.get(name)


def _timeout(cache: Any, timeout: Any) -> Any:
    """The TTL in seconds: the explicit one, or the backend default (0 means never expires)."""
    if timeout is not None:
        return timeout
    try:
        return getattr(cache.cache, "default_timeout", None)
    except (AttributeError, KeyError, RuntimeError):  # no app or cache not initialized
        return None


def _backend(cache: Any) -> str | None:
    try:
        return type(cache.cache).__name__
    except (AttributeError, KeyError, RuntimeError):  # no app or cache not initialized
        return None
