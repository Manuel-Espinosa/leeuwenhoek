"""Inserts, updates and deletes made through the SQLAlchemy ORM (any ``Session``)."""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from flask import current_app, has_app_context
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, SessionTransaction

from ..filters import MASK, limit_payload
from ..models import metadata as own_metadata
from .base import Watcher

log = logging.getLogger("leeuwenhoek")

#: ``session.info`` key of the changes flushed but not committed yet, per watcher
PENDING = "leeuwenhoek.models"
#: tables of Leeuwenhoek's own storage start with this
OWN_PREFIX = "leeuwenhoek_"

CREATED, UPDATED, DELETED = "created", "updated", "deleted"

# Session events and the method handling each one.
_EVENTS = (
    ("after_flush", "_after_flush"),
    ("after_commit", "_after_commit"),
    ("after_soft_rollback", "_after_soft_rollback"),
    ("after_transaction_end", "_after_transaction_end"),
)


class ModelWatcher(Watcher):
    name = "model"
    entry_type = "model"
    label = "Models"

    def __init__(self, lw) -> None:
        super().__init__(lw)
        self._listeners: list[tuple[str, Any]] = []

    def register(self, app) -> None:
        # Listening on the ``Session`` class catches every session: Flask-SQLAlchemy's scoped
        # session, ``sessionmaker`` factories and plain ``Session(engine)`` alike.
        for name, method in _EVENTS:
            fn = getattr(self, method)
            event.listen(Session, name, fn)
            self._listeners.append((name, fn))

    def unregister(self) -> None:
        for name, fn in self._listeners:
            event.remove(Session, name, fn)
        self._listeners = []

    # ----------------------------------------------------------------- events

    def _after_flush(self, session: Session, flush_context: Any) -> None:
        """Capture what the flush wrote; it is recorded when the transaction commits.

        History is only available until the flush finishes, so the content is built here and
        kept in ``session.info`` with the transaction it was flushed in.
        """
        try:
            if not self._watching(session):
                return
            changes = list(self._changes(session))
            if not changes:
                return
            tx = session.get_nested_transaction() or session.get_transaction()
            pending = session.info.setdefault(PENDING, {}).setdefault(self, [])
            pending.extend((tx, content) for content in changes)
        except Exception:  # recording must never break the app's flush
            log.exception("Could not capture ORM changes")

    def _after_commit(self, session: Session) -> None:
        """The outermost transaction committed: record what it flushed."""
        pending = session.info.get(PENDING, {}).pop(self, [])
        for _, content in pending:
            self.record(content, tags=[f"model:{content['model']}"])

    def _after_soft_rollback(self, session: Session, previous: SessionTransaction) -> None:
        """Drop the changes flushed in the rolled back transaction or a savepoint inside it."""
        pending = session.info.get(PENDING, {}).get(self)
        if pending:
            pending[:] = [(tx, c) for tx, c in pending if not _within(tx, previous)]

    def _after_transaction_end(self, session: Session, transaction: SessionTransaction) -> None:
        """The outermost transaction ended without a commit (``close()``): forget its changes."""
        if transaction.parent is None:
            session.info.get(PENDING, {}).pop(self, None)

    # ---------------------------------------------------------------- capture

    def _watching(self, session: Session) -> bool:
        batch = self.lw.current()
        if batch is not None and not batch.recording:
            return False
        if has_app_context() and current_app.extensions.get("leeuwenhoek") is not self.lw:
            return False  # another app's instance records it
        own = getattr(self.lw.storage, "engine", None)
        return own is None or session.bind is not own

    def _changes(self, session: Session) -> Iterable[dict[str, Any]]:
        cfg = self.lw.config
        groups = ((CREATED, session.new), (UPDATED, session.dirty), (DELETED, session.deleted))
        for action, objects in groups:
            for obj in objects:
                if _own(obj):
                    continue
                if action == UPDATED and not session.is_modified(obj, include_collections=False):
                    continue
                content = describe(obj, action, cfg.hidden_params, cfg.max_body_kb)
                if action == UPDATED and not content["changes"]:
                    continue
                yield content


def describe(obj: Any, action: str, hidden: Iterable[str], max_kb: int) -> dict[str, Any]:
    """Model, table, primary key and changed column values of one flushed object.

    Only column attributes are read, from the instance state (never loading anything):
    relationships are skipped, their foreign keys show the link. For ``created`` every loaded
    column is a change from ``None``; for ``deleted`` every loaded column goes to ``None``.
    """
    state = inspect(obj)
    mapper = state.mapper
    hidden_lower = {h.lower() for h in hidden}
    loaded = state.dict
    changes: dict[str, dict[str, Any]] = {}
    for attr in mapper.column_attrs:
        key = attr.key
        if action == UPDATED:
            history = state.attrs[key].history
            if not history.has_changes():
                continue
            old = history.deleted[0] if history.deleted else None
            new = history.added[0] if history.added else None
        elif key not in loaded:
            continue
        elif action == CREATED:
            old, new = None, loaded[key]
        else:
            old, new = loaded[key], None
        if key.lower() in hidden_lower:
            old = MASK if old is not None else None
            new = MASK if new is not None else None
        changes[key] = {"old": _value(old, max_kb), "new": _value(new, max_kb)}

    table = mapper.persist_selectable
    return {
        "model": mapper.class_.__name__,
        "table": getattr(table, "fullname", None) or str(table),
        "action": action,
        "key": _primary_key(state),
        "changes": changes,
    }


def _primary_key(state: Any) -> Any:
    """The primary key from the loaded values; one value, or a list for a composite key."""
    mapper, loaded = state.mapper, state.dict
    values = []
    for column in mapper.primary_key:
        prop = mapper.get_property_by_column(column)
        values.append(_value(loaded.get(prop.key), 1))
    return values[0] if len(values) == 1 else values


def _value(value: Any, max_kb: int) -> Any:
    """A column value as plain JSON, truncated to ``max_kb``."""
    if value is None or isinstance(value, (bool, int, float, str)):
        plain: Any = value
    elif isinstance(value, Decimal):
        plain = str(value)
    elif isinstance(value, (datetime, date, time)):
        plain = value.isoformat()
    elif isinstance(value, timedelta):
        plain = value.total_seconds()
    elif isinstance(value, uuid.UUID):
        plain = str(value)
    elif isinstance(value, enum.Enum):
        plain = value.value if isinstance(value.value, (str, int, float)) else value.name
    elif isinstance(value, (bytes, bytearray, memoryview)):
        plain = f"<{len(value)} bytes>"
    elif isinstance(value, (dict, list, tuple)):
        plain = value  # JSON columns; anything inside that isn't JSON goes through repr
    else:
        plain = repr(value)
    return limit_payload(plain, max_kb)


def _own(obj: Any) -> bool:
    """Whether the object is mapped onto one of Leeuwenhoek's own tables."""
    table = inspect(obj).mapper.persist_selectable
    name = getattr(table, "name", "")
    return getattr(table, "metadata", None) is own_metadata or name.startswith(OWN_PREFIX)


def _within(tx: SessionTransaction | None, ancestor: SessionTransaction) -> bool:
    while tx is not None:
        if tx is ancestor:
            return True
        tx = tx.parent
    return False
