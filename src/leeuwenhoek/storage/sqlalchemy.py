"""SQL driver (SQLite by default) with its own engine."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    Engine,
    String,
    cast,
    create_engine,
    delete,
    event,
    exists,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

from ..models import Entry, dumps, entries_table, metadata, settings_table, tags_table, utcnow
from .base import StorageDriver

e, t = entries_table.c, tags_table.c


class SQLAlchemyStorage(StorageDriver):
    def __init__(self, uri: str) -> None:
        url = make_url(uri)
        kwargs: dict[str, Any] = {"json_serializer": dumps}
        sqlite = url.get_backend_name() == "sqlite"
        if sqlite:
            kwargs["connect_args"] = {"check_same_thread": False}
            if url.database in (None, "", ":memory:"):
                kwargs["poolclass"] = StaticPool
            else:
                os.makedirs(os.path.dirname(os.path.abspath(url.database)), exist_ok=True)
        self.engine: Engine = create_engine(url, **kwargs)
        if sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)

    def install(self) -> None:
        metadata.create_all(self.engine)

    def save(self, entries: list[Entry]) -> None:
        if not entries:
            return
        rows = [
            {
                "uuid": x.uuid,
                "batch_id": x.batch_id,
                "parent_batch_id": x.parent_batch_id,
                "family_hash": x.family_hash,
                "type": x.type,
                "content": x.content,
                "is_root": x.is_root,
                "created_at": x.created_at,
            }
            for x in entries
        ]
        tags = [{"entry_uuid": x.uuid, "tag": tag} for x in entries for tag in x.tags]
        with self.engine.begin() as conn:
            conn.execute(insert(entries_table), rows)
            if tags:
                conn.execute(insert(tags_table), tags)

    def list(
        self,
        type=None,
        *,
        root_only=False,
        tag=None,
        q=None,
        before=None,
        after=None,
        limit=50,
    ) -> list[Entry]:
        stmt = select(entries_table)
        if type:
            stmt = stmt.where(e.type == type)
        if root_only:
            stmt = stmt.where(e.is_root.is_(True))
        if tag:
            stmt = stmt.where(exists().where(t.entry_uuid == e.uuid, t.tag == tag))
        if q:
            stmt = stmt.where(
                func.lower(cast(e.content, String)).contains(q.lower(), autoescape=True)
            )
        if before is not None:
            stmt = stmt.where(e.id < before)
        if after is not None:
            stmt = stmt.where(e.id > after).order_by(e.id.asc()).limit(limit)
            with self.engine.connect() as conn:
                rows = conn.execute(stmt).mappings().all()
            return self._hydrate(list(reversed(rows)))
        stmt = stmt.order_by(e.id.desc()).limit(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return self._hydrate(rows)

    def get(self, uuid: str) -> Entry | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(entries_table).where(e.uuid == uuid)).mappings().first()
        return self._hydrate([row])[0] if row else None

    def batch(self, batch_id: str) -> list[Entry]:
        stmt = select(entries_table).where(e.batch_id == batch_id).order_by(e.id.asc())
        with self.engine.connect() as conn:
            return self._hydrate(conn.execute(stmt).mappings().all())

    def family(self, family_hash: str, limit: int = 100) -> list[Entry]:
        stmt = (
            select(entries_table)
            .where(e.family_hash == family_hash)
            .order_by(e.id.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return self._hydrate(conn.execute(stmt).mappings().all())

    def counts(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            counts = dict(conn.execute(select(e.type, func.count()).group_by(e.type)).all())
            counts["batches"] = conn.execute(
                select(func.count()).where(e.is_root.is_(True))
            ).scalar_one()
        return counts

    def prune(self, hours: float) -> int:
        cutoff = utcnow() - timedelta(hours=hours)
        old = select(e.uuid).where(e.created_at < cutoff)
        with self.engine.begin() as conn:
            conn.execute(delete(tags_table).where(t.entry_uuid.in_(old)))
            return conn.execute(delete(entries_table).where(e.created_at < cutoff)).rowcount

    def clear(self) -> int:
        with self.engine.begin() as conn:
            conn.execute(delete(tags_table))
            return conn.execute(delete(entries_table)).rowcount

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        stmt = select(settings_table.c.value).where(settings_table.c.key == key)
        with self.engine.connect() as conn:
            value = conn.execute(stmt).scalar_one_or_none()
        return default if value is None else value

    def set_setting(self, key: str, value: str) -> None:
        with self.engine.begin() as conn:
            updated = conn.execute(
                update(settings_table).where(settings_table.c.key == key).values(value=value)
            ).rowcount
            if not updated:
                conn.execute(insert(settings_table).values(key=key, value=value))

    def close(self) -> None:
        self.engine.dispose()

    def _hydrate(self, rows) -> list[Entry]:
        if not rows:
            return []
        uuids = [r["uuid"] for r in rows]
        tags: dict[str, list[str]] = {}
        with self.engine.connect() as conn:
            for entry_uuid, tag in conn.execute(
                select(t.entry_uuid, t.tag).where(t.entry_uuid.in_(uuids))
            ):
                tags.setdefault(entry_uuid, []).append(tag)
        return [
            Entry(
                id=r["id"],
                uuid=r["uuid"],
                batch_id=r["batch_id"],
                parent_batch_id=r["parent_batch_id"],
                family_hash=r["family_hash"],
                type=r["type"],
                content=r["content"],
                is_root=r["is_root"],
                created_at=r["created_at"],
                tags=tags.get(r["uuid"], []),
            )
            for r in rows
        ]


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
