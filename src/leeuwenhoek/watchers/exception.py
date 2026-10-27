"""Unhandled exceptions raised by requests."""

from __future__ import annotations

import hashlib
import linecache
import traceback
from typing import Any

from flask import Flask, got_request_exception

from .base import Watcher

CONTEXT_LINES = 5


class ExceptionWatcher(Watcher):
    name = "exception"
    entry_type = "exception"
    label = "Exceptions"

    def register(self, app: Flask) -> None:
        self._app = app
        got_request_exception.connect(self._on_exception, app)

    def unregister(self) -> None:
        got_request_exception.disconnect(self._on_exception, self._app)

    def _on_exception(self, sender: Flask, exception: BaseException, **_: Any) -> None:
        self.capture(exception)

    def capture(self, exc: BaseException) -> None:
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
