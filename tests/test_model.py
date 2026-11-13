from datetime import datetime
from decimal import Decimal

import pytest
from conftest import entries
from sqlalchemy import ForeignKey, LargeBinary, Numeric, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.filters import MASK
from leeuwenhoek.models import entries_table
from leeuwenhoek.watchers.model import ModelWatcher


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    password: Mapped[str | None] = mapped_column(String(50))
    books: Mapped[list["Book"]] = relationship(back_populates="author")


class Book(Base):
    __tablename__ = "books"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    published: Mapped[datetime | None]
    cover: Mapped[bytes | None] = mapped_column(LargeBinary)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("authors.id"))
    author: Mapped[Author | None] = relationship(back_populates="books")


class OwnEntry(Base):
    """An ORM class on Leeuwenhoek's own table, as an app could (wrongly) map it."""

    __table__ = entries_table.to_metadata(Base.metadata)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Author.__table__, Book.__table__])
    yield engine
    engine.dispose()


@pytest.fixture
def models_app(app, lw, db):
    @app.post("/authors")
    def create():
        with Session(db) as s:
            s.add(Author(id=1, name="Ann", password="hunter2"))
            s.commit()
        return "ok"

    @app.post("/authors/rename")
    def rename():
        with Session(db) as s:
            author = s.get(Author, 1)
            author.name = "Anne"
            author.password = "secret2"
            s.commit()
        return "ok"

    @app.post("/authors/delete")
    def remove():
        with Session(db) as s:
            s.delete(s.get(Author, 1))
            s.commit()
        return "ok"

    return app


def models(lw):
    return list(reversed(entries(lw, "model")))


def test_create_update_delete_in_a_request(models_app, client, lw):
    client.post("/authors")
    client.post("/authors/rename")
    client.post("/authors/delete")
    created, updated, deleted = (e.content for e in models(lw))

    assert created["model"] == "Author" and created["table"] == "authors"
    assert created["action"] == "created" and created["key"] == 1
    assert created["changes"]["name"] == {"old": None, "new": "Ann"}

    assert updated["action"] == "updated" and updated["key"] == 1
    assert updated["changes"]["name"] == {"old": "Ann", "new": "Anne"}
    assert set(updated["changes"]) == {"name", "password"}  # only what changed

    assert deleted["action"] == "deleted" and deleted["key"] == 1
    assert deleted["changes"]["name"] == {"old": "Anne", "new": None}

    requests = list(reversed(entries(lw, "request")))
    assert [e.batch_id for e in models(lw)] == [r.batch_id for r in requests]
    assert all("model:Author" in e.tags for e in models(lw))


def test_hidden_params_are_redacted(models_app, client, lw):
    client.post("/authors")
    client.post("/authors/rename")
    created, updated = (e.content for e in models(lw))
    assert created["changes"]["password"] == {"old": None, "new": MASK}
    assert updated["changes"]["password"] == {"old": MASK, "new": MASK}
    assert all("hunter2" not in str(e.content) for e in models(lw))


def test_autoincrement_key_and_values_are_serialized(app, lw, db):
    with app.app_context(), Session(db) as s:
        author = Author(name="Bo")
        s.add(author)
        s.flush()
        s.add(
            Book(
                title="T",
                price=Decimal("9.50"),
                published=datetime(2026, 1, 2, 3, 4, 5),
                cover=b"\x00" * 10,
                author=author,
            )
        )
        s.commit()
    author_entry, book_entry = (e.content for e in models(lw))
    assert isinstance(author_entry["key"], int)
    book = book_entry["changes"]
    assert book["price"]["new"] == "9.50"
    assert book["published"]["new"] == "2026-01-02T03:04:05"
    assert book["cover"]["new"] == "<10 bytes>"
    assert book["author_id"]["new"] == author_entry["key"]
    assert "author" not in book  # relationships are skipped


def test_unchanged_dirty_objects_are_skipped(app, lw, db):
    with app.app_context(), Session(db) as s:
        s.add(Author(id=1, name="Ann"))
        s.commit()
        author = s.get(Author, 1)
        author.name = "Ann"  # same value: in session.dirty but not modified
        s.commit()
    assert [e.content["action"] for e in models(lw)] == ["created"]


def test_large_values_are_truncated(app, lw, db):
    app.config["LEEUWENHOEK_MAX_BODY_KB"] = 1
    lw.config.max_body_kb = 1
    with app.app_context(), Session(db) as s:
        s.add(Book(id=1, title="x" * 150))
        s.commit()
    title = models(lw)[0].content["changes"]["title"]["new"]
    assert title.startswith("x" * 100) and "truncated" not in title

    with app.app_context(), Session(db) as s:
        s.get(Book, 1).title = "y" * 5000
        s.commit()
    title = models(lw)[1].content["changes"]["title"]["new"]
    assert len(title) < 2000 and "truncated: 5000 characters" in title


def test_rolled_back_changes_are_not_recorded(app, lw, db):
    with app.app_context(), Session(db) as s:
        s.add(Author(id=1, name="Gone"))
        s.flush()
        s.rollback()
        s.add(Author(id=2, name="Kept"))
        s.flush()
    # closed without commit: nothing either
    assert models(lw) == []

    with app.app_context(), Session(db) as s:
        s.add(Author(id=3, name="Outer"))
        nested = s.begin_nested()
        s.add(Author(id=4, name="Savepoint"))
        s.flush()
        nested.rollback()
        with s.begin_nested():
            s.add(Author(id=5, name="Released"))
        s.commit()
    assert [e.content["changes"]["name"]["new"] for e in models(lw)] == ["Outer", "Released"]


def test_standalone_outside_a_request(app, lw, db):
    with app.app_context(), Session(db) as s:
        s.add(Author(id=1, name="Ann"))
        s.commit()
    (entry,) = models(lw)
    assert entry.is_root and entry.content["action"] == "created"
    assert entries(lw, "request") == []


def test_panel_listing_detail_and_related_card(models_app, client, lw):
    client.post("/authors")
    client.post("/authors/rename")
    listing = client.get("/leeuwenhoek/models")
    assert listing.status_code == 200
    assert "Author" in listing.text and "updated" in listing.text
    created, updated = models(lw)
    detail = client.get(f"/leeuwenhoek/models/{updated.uuid}").text
    assert "Anne" in detail and "Ann" in detail and MASK in detail
    req = next(r for r in entries(lw, "request") if r.batch_id == created.batch_id)
    page = client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    assert "Models" in page and "Author" in page
    assert client.get(f"/leeuwenhoek/batch/{req.batch_id}").status_code == 200
    assert "Models" in client.get("/leeuwenhoek/requests").text  # menu


def test_own_storage_is_never_recorded(app, db, tmp_path):
    app.config.update(
        LEEUWENHOEK_STORAGE="sqlalchemy",
        LEEUWENHOEK_DATABASE_URI=f"sqlite:///{tmp_path / 'lw.db'}",
    )
    ext = Leeuwenhoek(app)

    @app.post("/write")
    def write():
        with Session(db) as s:
            s.add(Author(id=1, name="Ann"))
            s.commit()
        # an ORM session on Leeuwenhoek's own engine, and a model on its own table
        with Session(ext.storage.engine) as s:
            s.add(
                OwnEntry(
                    uuid="u",
                    batch_id="b",
                    type="dump",
                    content={},
                    is_root=True,
                    created_at=datetime(2026, 1, 1),
                )
            )
            s.commit()
        return "ok"

    try:
        app.test_client().post("/write")
        ext.flush()
        found = ext.storage.list("model", limit=100)
        assert [e.content["model"] for e in found] == ["Author"]
    finally:
        ext.close()


def test_own_table_mapped_on_the_app_engine_is_skipped(app, lw):
    engine = create_engine("sqlite://")
    entries_table.create(engine)
    with app.app_context(), Session(engine) as s:
        s.add(
            OwnEntry(
                uuid="u",
                batch_id="b",
                type="dump",
                content={},
                is_root=True,
                created_at=datetime(2026, 1, 1),
            )
        )
        s.commit()
    assert models(lw) == []


def test_other_app_instance_does_not_record(app, lw, db):
    from flask import Flask

    other = Flask("other")
    other.config.update(
        LEEUWENHOEK_ENABLED=True, LEEUWENHOEK_STORAGE="memory", LEEUWENHOEK_ASYNC_WRITE=False
    )
    other_lw = Leeuwenhoek(other)
    try:
        with other.app_context(), Session(db) as s:
            s.add(Author(id=1, name="Ann"))
            s.commit()
        assert models(lw) == []
        assert len(entries(other_lw, "model")) == 1
    finally:
        other_lw.close()


def test_paused_batch_records_nothing(models_app, client, lw):
    lw.set_recording(False)
    client.post("/authors")
    assert models(lw) == []


def test_unregister_removes_the_listeners(app, lw, db):
    watcher = lw.watchers["model"]
    assert isinstance(watcher, ModelWatcher)
    assert event.contains(Session, "after_flush", watcher._after_flush)
    watcher.unregister()
    assert not event.contains(Session, "after_flush", watcher._after_flush)
    assert not event.contains(Session, "after_commit", watcher._after_commit)
    with app.app_context(), Session(db) as s:
        s.add(Author(id=1, name="Ann"))
        s.commit()
    assert models(lw) == []
    watcher.unregister()  # idempotent
