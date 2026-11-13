"""SQLAlchemy queries on the app engine."""

from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable
from types import FrameType
from typing import Any

from sqlalchemy import Engine, event

from ..context import Batch
from ..filters import redact
from ..models import Entry
from .base import Watcher

_SKIP = tuple(
    f"{os.sep}{pkg}{os.sep}"
    for pkg in ("sqlalchemy", "flask", "werkzeug", "flask_sqlalchemy", "leeuwenhoek")
)
MAX_FRAMES = 40
STACK = "leeuwenhoek.query_start"
N_PLUS_ONE_TAG = "n+1"

#: SQL keywords: highlighted in the panel and upper-cased by ``normalize_sql``.
KEYWORDS = [
    "SELECT",
    "INSERT",
    "INTO",
    "UPDATE",
    "DELETE",
    "FROM",
    "WHERE",
    "AND",
    "OR",
    "NOT",
    "NULL",
    "IS",
    "IN",
    "LIKE",
    "JOIN",
    "LEFT",
    "RIGHT",
    "INNER",
    "OUTER",
    "ON",
    "GROUP",
    "BY",
    "ORDER",
    "HAVING",
    "LIMIT",
    "OFFSET",
    "AS",
    "VALUES",
    "SET",
    "CREATE",
    "TABLE",
    "DROP",
    "ALTER",
    "INDEX",
    "DISTINCT",
    "COUNT",
    "UNION",
    "ALL",
    "CASE",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "BETWEEN",
    "EXISTS",
    "ASC",
    "DESC",
    "PRAGMA",
    "RETURNING",
]
KEYWORD_RE = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)

# Applied in order by ``normalize_sql``. String literals go first so nothing inside them is
# mistaken for a parameter or a number.
_NORMALIZE = [
    (re.compile(r"'(?:[^']|'')*'"), "?"),  # 'text', with '' escapes
    (re.compile(r"%\(\w+\)s|%s|(?<!:):\w+|\$\d+"), "?"),  # %(name)s, %s, :name, $1
    (re.compile(r"(?<![\w$\"])(?:0x[0-9a-f]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)\b", re.IGNORECASE), "?"),
    (re.compile(r"\s+"), " "),
    (re.compile(r"\(\s*\?(?:\s*,\s*\?)*\s*\)"), "(?)"),  # IN (?, ?, ?) -> IN (?)
    (re.compile(r"\(\?\)(?:\s*,\s*\(\?\))+"), "(?)"),  # VALUES (?), (?) -> VALUES (?)
]


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

        self.lw.on_finish.append(self._detect_n_plus_one)

    def unregister(self) -> None:
        for target in self._targets:
            event.remove(target, "before_cursor_execute", self._before)
            event.remove(target, "after_cursor_execute", self._after)
        self._targets = []
        if self._detect_n_plus_one in self.lw.on_finish:
            self.lw.on_finish.remove(self._detect_n_plus_one)

    def _detect_n_plus_one(self, batch: Batch, exc: BaseException | None) -> None:
        """Flag the queries repeated at least ``N_PLUS_ONE_THRESHOLD`` times in the batch.

        Runs as an ``on_finish`` callback, so before the batch is persisted and therefore before
        entry filters and taggers: they already see ``content["n_plus_one"]`` and the tag.
        """
        flag_n_plus_one(batch.entries, self.lw.config.n_plus_one_threshold)

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
        slow = duration > self.lw.config.slow_query_ms
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
                "slow": slow,
            },
            tags=["slow"] if slow else None,
        )


def _plain(parameters: Any) -> Any:
    if isinstance(parameters, dict):
        return dict(parameters)
    if isinstance(parameters, (list, tuple)):
        return list(parameters)
    return parameters


def origin() -> tuple[str | None, int | None]:
    """First frame that belongs to neither SQLAlchemy, Flask nor this library."""
    frame: FrameType | None = sys._getframe(1)
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


def normalize_sql(sql: str) -> str:
    """The shape of a statement: literals and bound parameters become ``?``.

    ``... where id = 1`` and ``... where id = :id`` both give ``... WHERE id = ?``. Lists of
    parameters collapse to one (``IN (?)``), whitespace is collapsed and keywords upper-cased.
    """
    out = sql
    for pattern, repl in _NORMALIZE:
        out = pattern.sub(repl, out)
    return KEYWORD_RE.sub(lambda m: m.group(1).upper(), out.strip())


def flag_n_plus_one(entries: Iterable[Entry], threshold: int) -> int:
    """Mark the queries whose normalized SQL repeats ``threshold`` times or more.

    Each one gets ``content["n_plus_one"] = True`` and the ``n+1`` tag. A ``threshold`` of 0 (or
    less) disables detection. Returns how many entries were flagged.
    """
    if threshold <= 0:
        return 0
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        if entry.type == "query" and isinstance(entry.content.get("sql"), str):
            groups.setdefault(normalize_sql(entry.content["sql"]), []).append(entry)
    flagged = 0
    for group in groups.values():
        if len(group) < threshold:
            continue
        for entry in group:
            entry.content["n_plus_one"] = True
            if N_PLUS_ONE_TAG not in entry.tags:
                entry.tags.append(N_PLUS_ONE_TAG)
            flagged += 1
    return flagged


def n_plus_one_summary(entries: Iterable[Entry]) -> list[tuple[str, int]]:
    """``(normalized SQL, count)`` of the flagged queries, the most repeated first."""
    counts = Counter(
        normalize_sql(e.content["sql"])
        for e in entries
        if e.type == "query" and e.content.get("n_plus_one") and e.content.get("sql")
    )
    return counts.most_common()
