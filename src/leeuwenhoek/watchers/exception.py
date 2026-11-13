"""Unhandled exceptions: raised by requests, or uncaught in scripts, CLI commands and threads."""

from __future__ import annotations

import hashlib
import linecache
import logging
import sys
import threading
import traceback
from types import TracebackType
from typing import Any

from flask import Flask, got_request_exception
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

from .base import Watcher

CONTEXT_LINES = 5
#: attribute set on an exception once recorded, holding the ids of the watchers that recorded it
SEEN = "__leeuwenhoek_seen__"

log = logging.getLogger("leeuwenhoek")


class ExceptionWatcher(Watcher):
    name = "exception"
    entry_type = "exception"
    label = "Exceptions"

    def register(self, app: Flask) -> None:
        self._app = app
        got_request_exception.connect(self._on_exception, app)
        # Apps with ``register_error_handler(Exception, ...)`` never emit
        # ``got_request_exception``, so also watch what the error handlers turn into 5xx responses.
        self._original = app.handle_user_exception
        app.handle_user_exception = self._handle_user_exception  # type: ignore[method-assign]
        # Exceptions that nothing catches: scripts, ``flask`` CLI commands (Click lets anything
        # but its own exceptions propagate) and threads. Both hooks are chained, never replaced.
        self._active = True
        self._previous_excepthook = sys.excepthook
        self._previous_threading_excepthook = threading.excepthook
        sys.excepthook = self._excepthook
        threading.excepthook = self._threading_excepthook

    def unregister(self) -> None:
        got_request_exception.disconnect(self._on_exception, self._app)
        self._app.handle_user_exception = self._original  # type: ignore[method-assign]
        self._active = False
        # Restore the previous hooks if ours are still installed. If something was chained on
        # top since, unhooking would drop it: ours stays in the chain and just delegates.
        if sys.excepthook == self._excepthook:
            sys.excepthook = self._previous_excepthook
        if threading.excepthook == self._threading_excepthook:
            threading.excepthook = self._previous_threading_excepthook

    def _excepthook(
        self, exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        self._capture_uncaught(exc)
        self._previous_excepthook(exc_type, exc, tb)

    def _threading_excepthook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            self._capture_uncaught(args.exc_value)
        self._previous_threading_excepthook(args)

    def _capture_uncaught(self, exc: BaseException) -> None:
        # KeyboardInterrupt and friends are not errors of the app.
        if not self._active or not isinstance(exc, Exception):
            return
        try:
            self.capture(exc)
        except Exception:  # an excepthook must never fail
            log.exception("Could not record an uncaught exception")

    def _handle_user_exception(self, e: Exception) -> HTTPException | ResponseReturnValue:
        # Same signature as ``Flask.handle_user_exception``, which it replaces.
        result = self._original(e)  # re-raises when unhandled: the signal covers that case
        if status_of(result) >= self.lw.config.handled_exception_min_status:
            self.capture(e, handled=True)
        return result

    def _on_exception(self, sender: Flask, exception: BaseException, **_: Any) -> None:
        self.capture(exception)

    def capture(self, exc: BaseException, handled: bool = False) -> None:
        """Record ``exc`` once per watcher, even if it is reported again (request, then hook)."""
        if not self._first_time(exc):
            return
        frames = [
            {
                "file": f.filename,
                "line": f.lineno,
                "function": f.name,
                "code": f.line,
                "snippet": snippet(f.filename, f.lineno),
            }
            for f in traceback.extract_tb(exc.__traceback__)
        ]
        origin = frames[-1] if frames else {"file": None, "line": None}
        cls = f"{type(exc).__module__}.{type(exc).__qualname__}"
        family = hashlib.sha1(f"{cls}{origin['file']}{origin['line']}".encode()).hexdigest()
        content = {
            "class": cls,
            "message": str(exc),
            "handled": handled,
            "file": origin["file"],
            "line": origin["line"],
            "traceback": "".join(traceback.format_exception(exc)),
            "frames": frames,
        }
        self.record(
            content,
            tags=[f"exception:{type(exc).__name__}"],
            family_hash=family,
            is_root=True,
        )

    def _first_time(self, exc: BaseException) -> bool:
        seen = getattr(exc, SEEN, None)
        if seen is None:
            seen = set()
            try:
                setattr(exc, SEEN, seen)
            except (AttributeError, TypeError):  # cannot be marked: record it anyway
                return True
        if id(self) in seen:
            return False
        seen.add(id(self))
        return True


def status_of(result: Any) -> int:
    """Status code of whatever a Flask error handler returned (response, tuple, dict...)."""
    if isinstance(result, tuple):
        return next((x for x in result[1:] if isinstance(x, int)), 200)
    return getattr(result, "status_code", 200)


def snippet(filename: str | None, lineno: int | None) -> list[dict[str, Any]]:
    """±5 lines of code around ``lineno``."""
    if not filename or not lineno:
        return []
    start = max(lineno - CONTEXT_LINES, 1)
    lines = []
    for n in range(start, lineno + CONTEXT_LINES + 1):
        text = linecache.getline(filename, n)
        if not text:
            break
        lines.append({"line": n, "code": text.rstrip("\n"), "current": n == lineno})
    return lines
