"""Defaults and ``app.config`` reading."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from flask import Flask
from sqlalchemy.engine import URL

PREFIX = "LEEUWENHOEK_"

DEFAULTS: dict[str, Any] = {
    "ENABLED": None,  # None -> app.debug
    "URL_PREFIX": "/leeuwenhoek",
    "WATCHERS": None,  # None -> every watcher whose library is installed
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
    "POLL_SECONDS": 4,  # live polling interval in the panel; 0 disables it
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
    "TRACK_MEMORY": False,  # record each request's peak memory with tracemalloc
    "SLOW_QUERY_MS": 100,  # queries slower than this are flagged as slow
    "CACHE_VALUES": False,  # store the values read and written by the cache watcher
}


TRUE = {"1", "true", "yes", "on"}

#: Parts of the storage URL; each one can come from ``LEEUWENHOEK_DB_<PART>``.
DB_PARTS = ("DRIVER", "USERNAME", "PASSWORD", "HOST", "PORT", "DATABASE", "SOCKET")


def _coerce(key: str, default: Any, raw: str) -> Any:
    """Turn an environment string into the type of the key's default."""
    if key in ("ENABLED",) or isinstance(default, bool):
        return raw.strip().lower() in TRUE
    if key == "BASIC_AUTH":
        user, _, password = raw.partition(":")
        return (user, password)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if key == "WATCHERS" or isinstance(default, (list, dict)):
        try:
            return json.loads(raw)
        except ValueError:
            return [item.strip() for item in raw.split(",") if item.strip()]  # a, b, c
    return raw


def database_url(get: Callable[[str], Any]) -> URL | None:
    """Build the storage URL from ``DB_*`` parts; ``None`` if none is set.

    ``DB_SOCKET`` is a Cloud SQL instance connection name (``project:region:instance``).
    """
    parts = {part: get("DB_" + part) for part in DB_PARTS}
    if not (parts["DRIVER"] or parts["HOST"] or parts["SOCKET"]):
        return None
    driver = parts["DRIVER"] or "postgresql+psycopg"
    query: dict[str, str] = {}
    if parts["SOCKET"] and not parts["HOST"]:
        key = "unix_socket" if "mysql" in driver else "host"
        query[key] = f"/cloudsql/{parts['SOCKET']}"
    return URL.create(
        drivername=driver,
        username=parts["USERNAME"],
        password=parts["PASSWORD"],
        host=parts["HOST"],
        port=int(parts["PORT"]) if parts["PORT"] else None,
        database=parts["DATABASE"],
        query=query,
    )


class Config:
    """Snapshot of the effective configuration: ``cfg.max_body_kb``.

    Each ``LEEUWENHOEK_*`` key is read from ``app.config`` first and then from the environment,
    so an app can be configured with plain environment variables.
    """

    def __init__(self, app: Flask) -> None:
        def get(key: str) -> Any:
            if (value := app.config.get(PREFIX + key)) is not None:
                return value
            raw = os.environ.get(PREFIX + key, "")
            return _coerce(key, DEFAULTS.get(key), raw) if raw != "" else None

        self._values = dict(DEFAULTS)
        for key in DEFAULTS:
            value = get(key)
            if value is not None:
                self._values[key] = value
        if self._values["DATABASE_URI"] is None:
            url = database_url(get)
            self._values["DATABASE_URI"] = (
                url.render_as_string(hide_password=False)
                if url
                else f"sqlite:///{app.instance_path}/leeuwenhoek.db"
            )

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["_values"][name.upper()]
        except KeyError:
            raise AttributeError(name) from None
