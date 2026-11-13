from .base import Watcher
from .cache import CacheWatcher
from .dump import DumpWatcher
from .exception import ExceptionWatcher
from .log import LogWatcher
from .query import QueryWatcher
from .request import RequestWatcher

#: in the order they appear in the menu
AVAILABLE: dict[str, type[Watcher]] = {
    w.name: w
    for w in (
        RequestWatcher,
        DumpWatcher,
        ExceptionWatcher,
        LogWatcher,
        QueryWatcher,
        CacheWatcher,
    )
}


def default_watchers() -> list[str]:
    """Every watcher whose required modules are installed."""
    return [name for name, cls in AVAILABLE.items() if cls.available()]


__all__ = ["AVAILABLE", "Watcher", "default_watchers"]
