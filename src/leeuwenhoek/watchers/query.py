"""SQLAlchemy queries on the app engine."""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from sqlalchemy import Engine, event

from ..filters import redact
from .base import Watcher

_SKIP = tuple(
    f"{os.sep}{pkg}{os.sep}"
    for pkg in ("sqlalchemy", "flask", "werkzeug", "flask_sqlalchemy", "leeuwenhoek")
)
MAX_FRAMES = 40
STACK = "leeuwenhoek.query_start"


class QueryWatcher(Watcher):
    name = "query"
    entry_type = "query"
    label = "Queries"

    def __init__(self, lw, engine: Engine | list[Engine] | None = None) -> None:
        super().__init__(lw)
        self._engines = [engine] if isinstance(engine, Engine) else list(engine or [])
        self._targets: list[Any] = []

    def register(self, app) -> None:
        # Without an explicit engine, listen to all of them (except Leeuwenhoek's own).
        self._targets = self._engines or [Engine]
        for target in self._targets:
            event.listen(target, "before_cursor_execute", self._before)
            event.listen(target, "after_cursor_execute", self._after)

    def unregister(self) -> None:
        for target in self._targets:
            event.remove(target, "before_cursor_execute", self._before)
            event.remove(target, "after_cursor_execute", self._after)
        self._targets = []

    def _own(self, conn) -> bool:
        return conn.engine is getattr(self.lw.storage, "engine", None)

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if not self._own(conn):
            conn.info.setdefault(STACK, []).append(time.perf_counter())

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if self._own(conn):
            return
        started = conn.info[STACK].pop()
        batch = self.lw.current()
        if batch is not None and not batch.recording:
            return
        duration = (time.perf_counter() - started) * 1000
        params = redact(_plain(parameters), self.lw.config.hidden_params)
        file, line = origin()
        self.record(
            {
                "sql": statement,
                "parameters": params,
                "interpolated": interpolate(statement, params),
                "duration_ms": round(duration, 3),
                "engine": str(conn.engine.url.render_as_string(hide_password=True)),
                "file": file,
                "line": line,
                "many": executemany,
            }
        )


def _plain(parameters: Any) -> Any:
    if isinstance(parameters, dict):
        return dict(parameters)
    if isinstance(parameters, (list, tuple)):
        return list(parameters)
    return parameters


def origin() -> tuple[str | None, int | None]:
    """First frame that belongs to neither SQLAlchemy, Flask nor this library."""
    frame = sys._getframe(1)
    for _ in range(MAX_FRAMES):
        if frame is None:
            break
        filename = frame.f_code.co_filename
        if not any(part in filename for part in _SKIP) and not filename.startswith("<"):
            return filename, frame.f_lineno
        frame = frame.f_back
    return None, None


def interpolate(statement: str, params: Any) -> str | None:
    """SQL with parameters substituted. For display only, never for execution."""
    try:
        if isinstance(params, dict):
            out = statement
            for key, value in params.items():
                literal = _literal(value)
                out = out.replace(f"%({key})s", literal).replace(f":{key}", literal)
            return out
        if isinstance(params, list) and params and not isinstance(params[0], (list, tuple, dict)):
            out, values = statement, iter(params)
            for marker in ("?", "%s"):
                while marker in out:
                    out = out.replace(marker, _literal(next(values)), 1)
            return out
    except (StopIteration, TypeError):
        pass
    return None


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
