"""Logs: a dedicated handler on the root logger."""

from __future__ import annotations

import logging
import traceback

from flask import Flask

from .base import Watcher

# Standard LogRecord attributes; anything else is ``extra``.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
IGNORED_LOGGERS = ("leeuwenhoek", "werkzeug")


class _Handler(logging.Handler):
    def __init__(self, watcher: LogWatcher) -> None:
        super().__init__(level=logging.NOTSET)
        self.watcher = watcher

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".")[0] in IGNORED_LOGGERS:
            return
        try:
            exc = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None
            self.watcher.record(
                {
                    "level": record.levelname,
                    "message": record.getMessage(),
                    "logger": record.name,
                    "extra": {k: v for k, v in record.__dict__.items() if k not in _STANDARD},
                    "exc_info": exc,
                },
                tags=[f"level:{record.levelname}"],
            )
        except Exception:  # noqa: BLE001 - a log call must never break the app
            self.handleError(record)


class LogWatcher(Watcher):
    name = "log"
    entry_type = "log"
    label = "Logs"

    def register(self, app: Flask) -> None:
        self._handler = _Handler(self)
        logging.getLogger().addHandler(self._handler)

    def unregister(self) -> None:
        logging.getLogger().removeHandler(self._handler)
