"""``batch_id`` and buffer of the current cycle (contextvars)."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Literal

from .models import Entry, new_id

# ``job``: a unit of work dispatched by another batch (a queued task); ``schedule``: a run started
# by a scheduler's clock (APScheduler), which no batch dispatched.
BatchKind = Literal["request", "job", "command", "schedule", "standalone"]


@dataclass
class Batch:
    kind: BatchKind = "request"
    id: str = field(default_factory=new_id)
    parent_id: str | None = None
    entries: list[Entry] = field(default_factory=list)
    recording: bool = True  # False if the cycle was left out by a filter or by pausing
    max_entries: int = 500
    tags: list[str] = field(default_factory=list)  # propagated to every entry on persist
    meta: dict[str, Any] = field(default_factory=dict)  # data passed between hooks

    def add(self, entry: Entry, force: bool = False) -> bool:
        """``force`` bypasses the cap: the request entry must never be lost."""
        if not self.recording or (len(self.entries) >= self.max_entries and not force):
            return False
        self.entries.append(entry)
        return True


_current: contextvars.ContextVar[Batch | None] = contextvars.ContextVar(
    "leeuwenhoek_batch", default=None
)
# Keeps what Leeuwenhoek does internally from being recorded.
_internal: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "leeuwenhoek_internal", default=False
)


def current_batch() -> Batch | None:
    return _current.get()


def start_batch(batch: Batch) -> contextvars.Token:
    return _current.set(batch)


def end_batch(token: contextvars.Token) -> None:
    _current.reset(token)


def is_internal() -> bool:
    return _internal.get()


class internal:
    """Context manager: nothing that happens inside is recorded."""

    def __enter__(self) -> None:
        self._token = _internal.set(True)

    def __exit__(self, *exc: object) -> None:
        _internal.reset(self._token)
