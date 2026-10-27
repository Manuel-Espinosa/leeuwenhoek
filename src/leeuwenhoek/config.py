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
    "SAMPLE_RATE": 1.0,  # fraction of batches to record (0-1)
    "ONLY_ERRORS": False,  # keep only batches with an exception or a 5xx response
    "QUEUE_MAX_BATCHES": 1000,  # pending writes kept in memory before dropping
    "ENGINE_OPTIONS": {},  # extra create_engine() kwargs for the storage engine
    "AUTO_INSTALL": True,  # create tables on init_app
    "AUTO_PRUNE": True,  # prune in the background writer
    "PRUNE_INTERVAL_MINUTES": 30,
    "HANDLED_EXCEPTION_MIN_STATUS": 500,  # capture exceptions turned into responses >= this
    "FLUSH_TIMEOUT": 5.0,  # seconds to wait for pending writes at exit
    "REQUEST_TAGS": {},  # {"tag_name": "dotted.path" | ["path", ...]}
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
