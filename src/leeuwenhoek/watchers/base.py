"""Abstract watcher."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from flask import Flask

if TYPE_CHECKING:
    from .. import Leeuwenhoek


class Watcher(ABC):
    #: name in ``LEEUWENHOEK_WATCHERS``
    name: str
    #: entry type it produces (menu section)
    entry_type: str
    #: menu label
    label: str

    def __init__(self, lw: Leeuwenhoek) -> None:
        self.lw = lw

    @abstractmethod
    def register(self, app: Flask) -> None:
        """Attach hooks, signals or handlers."""

    def unregister(self) -> None:
        """Undo what ``register`` did outside the app (global handlers, listeners)."""

    def record(self, content: dict[str, Any], **kwargs: Any):
        return self.lw.record(self.entry_type, content, **kwargs)
