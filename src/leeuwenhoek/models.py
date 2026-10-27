"""Entries and tables."""

from __future__ import annotations

import json
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)


def utcnow() -> datetime:
    """Naive UTC: SQLite returns it that way and it compares the same in every driver."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuidlib.uuid4())


@dataclass
class Entry:
    type: str
    content: dict[str, Any]
    batch_id: str
    uuid: str = field(default_factory=new_id)
    parent_batch_id: str | None = None
    family_hash: str | None = None
    is_root: bool = False
    created_at: datetime = field(default_factory=utcnow)
    tags: list[str] = field(default_factory=list)
    id: int | None = None


def _default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return repr(value)


def dumps(value: Any) -> str:
    return json.dumps(value, default=_default)


def jsonable(value: Any) -> Any:
    """Normalize a payload to plain JSON types (anything unserializable goes through ``repr``)."""
    return json.loads(dumps(value))


metadata = MetaData()

# SQLite only autoincrements with INTEGER PRIMARY KEY.
_pk = BigInteger().with_variant(Integer, "sqlite")

entries_table = Table(
    "leeuwenhoek_entries",
    metadata,
    Column("id", _pk, primary_key=True, autoincrement=True),
    Column("uuid", String(36), unique=True, nullable=False),
    Column("batch_id", String(36), nullable=False, index=True),
    Column("parent_batch_id", String(36), nullable=True),
    Column("family_hash", String(40), nullable=True, index=True),
    Column("type", String(32), nullable=False, index=True),
    Column("content", JSON, nullable=False),
    Column("is_root", Boolean, nullable=False, default=False),
    Column("created_at", DateTime, nullable=False, index=True),
    Index("ix_leeuwenhoek_entries_type_created", "type", "created_at"),
)

tags_table = Table(
    "leeuwenhoek_tags",
    metadata,
    Column("entry_uuid", String(36), ForeignKey("leeuwenhoek_entries.uuid"), nullable=False),
    Column("tag", String(255), nullable=False, index=True),
)

settings_table = Table(
    "leeuwenhoek_settings",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", String(255), nullable=False),
)
