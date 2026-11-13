"""Storage driver interface."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import Entry


@dataclass
class Page:
    entries: list[Entry] = field(default_factory=list)
    older: int | None = None  # ``before`` cursor to go backwards
    newer: int | None = None  # ``after`` cursor to go forwards


class StorageDriver(ABC):
    @abstractmethod
    def install(self) -> None: ...

    @abstractmethod
    def save(self, entries: list[Entry]) -> None:
        """Persist all entries of a batch at once."""

    @abstractmethod
    def list(
        self,
        type: str | None = None,
        *,
        root_only: bool = False,
        tag: str | None = None,
        q: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = 50,
    ) -> builtins.list[Entry]:
        """Most recent first. ``after`` returns the next ``limit`` entries after that id."""

    @abstractmethod
    def get(self, uuid: str) -> Entry | None: ...

    @abstractmethod
    def batch(self, batch_id: str) -> builtins.list[Entry]:
        """Entries of a batch in chronological order."""

    @abstractmethod
    def family(self, family_hash: str, limit: int = 100) -> builtins.list[Entry]: ...

    @abstractmethod
    def counts(self) -> dict[str, int]:
        """Entries per type; the ``"batches"`` key counts root entries."""

    @abstractmethod
    def prune(self, hours: float) -> int: ...

    @abstractmethod
    def clear(self) -> int: ...

    @abstractmethod
    def get_setting(self, key: str, default: str | None = None) -> str | None: ...

    @abstractmethod
    def set_setting(self, key: str, value: str) -> None: ...

    def close(self) -> None:  # pragma: no cover - optional
        pass

    def page(
        self,
        type: str | None = None,
        *,
        root_only: bool = False,
        tag: str | None = None,
        q: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = 50,
    ) -> Page:
        """A page with cursors for the "Older" / "Newer" buttons."""
        rows = self.list(
            type, root_only=root_only, tag=tag, q=q, before=before, after=after, limit=limit + 1
        )
        if after is not None:
            has_newer = len(rows) > limit
            if has_newer:
                rows = rows[1:]  # the extra row is the newest one
            if not rows:
                return Page()
            older = rows[-1].id
            newer = rows[0].id if has_newer else None
            return Page(rows, older=older, newer=newer)
        has_older = len(rows) > limit
        rows = rows[:limit]
        if not rows:
            return Page()
        newer = None
        if before is not None and self.list(
            type, root_only=root_only, tag=tag, q=q, after=rows[0].id, limit=1
        ):
            newer = rows[0].id
        return Page(rows, older=rows[-1].id if has_older else None, newer=newer)
