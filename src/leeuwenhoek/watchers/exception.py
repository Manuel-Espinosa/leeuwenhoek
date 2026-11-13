"""Unhandled exceptions raised by requests."""

from __future__ import annotations

import hashlib
import linecache
import traceback
from typing import Any

from flask import Flask, got_request_exception
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

from .base import Watcher

CONTEXT_LINES = 5


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

    def unregister(self) -> None:
        got_request_exception.disconnect(self._on_exception, self._app)
        self._app.handle_user_exception = self._original  # type: ignore[method-assign]

    def _handle_user_exception(self, e: Exception) -> HTTPException | ResponseReturnValue:
        # Same signature as ``Flask.handle_user_exception``, which it replaces.
        result = self._original(e)  # re-raises when unhandled: the signal covers that case
        if status_of(result) >= self.lw.config.handled_exception_min_status:
            self.capture(e, handled=True)
        return result

    def _on_exception(self, sender: Flask, exception: BaseException, **_: Any) -> None:
        self.capture(exception)

    def capture(self, exc: BaseException, handled: bool = False) -> None:
        batch = self.lw.current()
        if batch is not None:
            seen = batch.meta.setdefault("captured", set())
            if id(exc) in seen:
                return
            seen.add(id(exc))
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
