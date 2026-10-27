from .base import Watcher
from .dump import DumpWatcher
from .exception import ExceptionWatcher
from .log import LogWatcher
from .query import QueryWatcher
from .request import RequestWatcher

#: in the order they appear in the menu
AVAILABLE: dict[str, type[Watcher]] = {
    w.name: w for w in (RequestWatcher, DumpWatcher, ExceptionWatcher, LogWatcher, QueryWatcher)
}

__all__ = ["AVAILABLE", "Watcher"]
