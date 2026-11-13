from .base import Watcher
from .cache import CacheWatcher
from .command import CommandWatcher
from .dump import DumpWatcher
from .exception import ExceptionWatcher
from .http_client import HttpClientWatcher
from .log import LogWatcher
from .mail import MailWatcher
from .model import ModelWatcher
from .query import QueryWatcher
from .request import RequestWatcher
from .template import TemplateWatcher

#: in the order they appear in the menu
AVAILABLE: dict[str, type[Watcher]] = {
    w.name: w
    for w in (
        RequestWatcher,
        DumpWatcher,
        ExceptionWatcher,
        LogWatcher,
        QueryWatcher,
        MailWatcher,
        TemplateWatcher,
        CommandWatcher,
        HttpClientWatcher,
        CacheWatcher,
        ModelWatcher,
    )
}


def default_watchers() -> list[str]:
    """Every watcher whose required modules are installed."""
    return [name for name, cls in AVAILABLE.items() if cls.available()]


__all__ = ["AVAILABLE", "Watcher", "default_watchers"]
