"""Defaults and ``app.config`` reading."""

from __future__ import annotations

from typing import Any

from flask import Flask

PREFIX = "LEEUWENHOEK_"

WATCHERS = ["request", "exception", "query", "log", "dump"]

DEFAULTS: dict[str, Any] = {
    "ENABLED": None,  # None -> app.debug
    "URL_PREFIX": "/leeuwenhoek",
    "WATCHERS": WATCHERS,
    "IGNORE_PATHS": ["/static*", "/favicon.ico", "/health*"],
    "HIDDEN_HEADERS": ["authorization", "cookie", "x-api-key"],
    "HIDDEN_PARAMS": ["password", "password_confirmation", "token", "secret"],
    "MAX_BODY_KB": 64,
    "MAX_ENTRIES_PER_BATCH": 500,
    "PRUNE_HOURS": 24,
    "DATABASE_URI": None,  # None -> sqlite in app.instance_path
    "STORAGE": "sqlalchemy",  # "sqlalchemy" | "memory"
    "ASYNC_WRITE": True,
    "BASIC_AUTH": None,
    "PAGE_SIZE": 50,
    "BULMA_CDN": False,
}


class Config:
    """Snapshot of the effective configuration: ``cfg.max_body_kb``."""

    def __init__(self, app: Flask) -> None:
        self._values = {
            key: app.config.get(PREFIX + key, default) for key, default in DEFAULTS.items()
        }
        if self._values["DATABASE_URI"] is None:
            self._values["DATABASE_URI"] = f"sqlite:///{app.instance_path}/leeuwenhoek.db"

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["_values"][name.upper()]
        except KeyError:
            raise AttributeError(name) from None
