"""In-memory driver (tests)."""

from __future__ import annotations

import threading
from collections import Counter
from datetime import timedelta

from ..models import Entry, dumps, utcnow
from .base import StorageDriver


class MemoryStorage(StorageDriver):
    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self._settings: dict[str, str] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def install(self) -> None:
        pass

    def save(self, entries: list[Entry]) -> None:
        with self._lock:
            for entry in entries:
                entry.id = self._next_id
                self._next_id += 1
                self._entries.append(entry)

    def list(
        self,
        type=None,
        *,
        root_only=False,
        tag=None,
        q=None,
        before=None,
        after=None,
        limit=50,
    ) -> list[Entry]:
        with self._lock:
            rows = list(self._entries)
        if type:
            rows = [e for e in rows if e.type == type]
        if root_only:
            rows = [e for e in rows if e.is_root]
        if tag:
            rows = [e for e in rows if tag in e.tags]
        if q:
            needle = q.lower()
            rows = [e for e in rows if needle in dumps(e.content).lower()]
        if before is not None:
            rows = [e for e in rows if e.id < before]
        if after is not None:
            rows = [e for e in rows if e.id > after]
            return list(reversed(rows[:limit]))
        return list(reversed(rows))[:limit]

    def get(self, uuid: str) -> Entry | None:
        with self._lock:
            return next((e for e in self._entries if e.uuid == uuid), None)

    def batch(self, batch_id: str) -> list[Entry]:
        with self._lock:
            return [e for e in self._entries if e.batch_id == batch_id]

    def family(self, family_hash: str, limit: int = 100) -> list[Entry]:
        with self._lock:
            rows = [e for e in self._entries if e.family_hash == family_hash]
        return list(reversed(rows))[:limit]

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts = Counter(e.type for e in self._entries)
            counts["batches"] = sum(1 for e in self._entries if e.is_root)
        return dict(counts)

    def prune(self, hours: float) -> int:
        cutoff = utcnow() - timedelta(hours=hours)
        with self._lock:
            kept = [e for e in self._entries if e.created_at >= cutoff]
            removed = len(self._entries) - len(kept)
            self._entries = kept
        return removed

    def clear(self) -> int:
        with self._lock:
            removed = len(self._entries)
            self._entries = []
        return removed

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        return self._settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self._settings[key] = value
