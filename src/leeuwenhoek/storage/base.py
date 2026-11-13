"""Storage driver interface."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime

from ..models import Entry, utcnow

#: settings key prefix of a resolved exception family; the value is the resolve time (ISO, UTC)
RESOLVED_PREFIX = "resolved:"


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
        exclude_families: Collection[str] | None = None,
    ) -> builtins.list[Entry]:
        """Most recent first. ``after`` returns the next ``limit`` entries after that id.

        ``exclude_families`` leaves out the entries of those ``family_hash`` values (entries
        without a family are always kept).
        """

    @abstractmethod
    def get(self, uuid: str) -> Entry | None: ...

    @abstractmethod
    def batch(self, batch_id: str) -> builtins.list[Entry]:
        """Entries of a batch in chronological order."""

    @abstractmethod
    def family(self, family_hash: str, limit: int = 100) -> builtins.list[Entry]: ...

    @abstractmethod
    def last_seen(self, family_hashes: Collection[str]) -> dict[str, datetime]:
        """``created_at`` of the newest entry of each family, in one query.

        Families without entries are left out.
        """

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

    @abstractmethod
    def delete_setting(self, key: str) -> None:
        """Remove a setting; nothing happens if it doesn't exist."""

    @abstractmethod
    def settings(self, prefix: str = "") -> dict[str, str]:
        """Every setting whose key starts with ``prefix``."""

    # ---------------------------------------------------------- resolved families

    def resolve_family(self, family_hash: str, at: datetime | None = None) -> None:
        """Mark an exception family as resolved at ``at`` (now by default)."""
        self.set_setting(RESOLVED_PREFIX + family_hash, (at or utcnow()).isoformat())

    def reopen_family(self, family_hash: str) -> None:
        self.delete_setting(RESOLVED_PREFIX + family_hash)

    def family_resolved_at(self, family_hash: str) -> datetime | None:
        """When the family was resolved, or ``None`` if it is open (or has reopened)."""
        value = self.get_setting(RESOLVED_PREFIX + family_hash)
        if value is None:
            return None
        at = datetime.fromisoformat(value)
        seen = self.last_seen([family_hash]).get(family_hash)
        return at if seen is not None and seen <= at else None

    def resolved_families(self) -> dict[str, datetime]:
        """Families that are still resolved, with their resolve time.

        A family reopens lazily: it stops being resolved as soon as it has an occurrence newer
        than its resolve time. Nothing is written when an exception is recorded, so the writer
        thread never races with the panel, and two settings queries plus one grouped query
        answer for every family at once.
        """
        resolved = {
            key[len(RESOLVED_PREFIX) :]: datetime.fromisoformat(value)
            for key, value in self.settings(RESOLVED_PREFIX).items()
        }
        if not resolved:
            return {}
        seen = self.last_seen(resolved)
        return {
            family: at for family, at in resolved.items() if family in seen and seen[family] <= at
        }

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
        exclude_families: Collection[str] | None = None,
    ) -> Page:
        """A page with cursors for the "Older" / "Newer" buttons."""
        filters: dict = {"tag": tag, "q": q, "exclude_families": exclude_families}
        rows = self.list(
            type, root_only=root_only, before=before, after=after, limit=limit + 1, **filters
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
            type, root_only=root_only, after=rows[0].id, limit=1, **filters
        ):
            newer = rows[0].id
        return Page(rows, older=rows[-1].id if has_older else None, newer=newer)
