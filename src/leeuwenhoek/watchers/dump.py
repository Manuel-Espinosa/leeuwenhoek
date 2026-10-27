"""``leeuwenhoek.dump(var)``."""

from __future__ import annotations

import pprint
import sys
from typing import Any

from flask import Flask, current_app, has_app_context

from .base import Watcher


class DumpWatcher(Watcher):
    name = "dump"
    entry_type = "dump"
    label = "Dumps"

    def register(self, app: Flask) -> None:
        pass  # invoked explicitly from dump()


def dump(obj: Any) -> Any:
    """Record ``obj`` in the panel and return it (can be chained)."""
    if not has_app_context():
        return obj
    lw = current_app.extensions.get("leeuwenhoek")
    watcher = lw.watchers.get("dump") if lw else None
    if watcher is None:
        return obj
    frame = sys._getframe(1)
    watcher.record(
        {
            "type": type(obj).__qualname__,
            "repr": pprint.pformat(obj, width=100),
            "file": frame.f_code.co_filename,
            "line": frame.f_lineno,
        },
        is_root=True,
    )
    return obj
