"""Abstract watcher."""

from __future__ import annotations

from abc import ABC, abstractmethod
from importlib.util import find_spec
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
    #: module(s) the watcher needs, e.g. ``"flask_mail"``; empty or None means always available.
    #: Import them inside ``register`` so the watcher module loads without them.
    requires: str | tuple[str, ...] | None = None

    @classmethod
    def missing(cls) -> list[str]:
        """Required modules that are not installed."""
        modules = (cls.requires,) if isinstance(cls.requires, str) else cls.requires or ()
        return [module for module in modules if module and not _installed(module)]

    @classmethod
    def available(cls) -> bool:
        """Whether every required module is installed."""
        return not cls.missing()

    def __init__(self, lw: Leeuwenhoek) -> None:
        self.lw = lw

    @abstractmethod
    def register(self, app: Flask) -> None:
        """Attach hooks, signals or handlers."""

    def unregister(self) -> None:
        """Undo what ``register`` did outside the app (global handlers, listeners)."""

    def record(self, content: dict[str, Any], **kwargs: Any):
        return self.lw.record(self.entry_type, content, **kwargs)


def _installed(module: str) -> bool:
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):  # missing parent package of a dotted name
        return False
