import os
import threading
import time
from datetime import timedelta

import pytest
from conftest import entries

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.models import Entry, utcnow
from leeuwenhoek.storage import SQLAlchemyStorage
from leeuwenhoek.writer import Writer


def make(app, engine, **config):
    app.config.update({f"LEEUWENHOEK_{k.upper()}": v for k, v in config.items()})
    return Leeuwenhoek(app, engine=engine)


def test_handled_exception_captured_once(app, engine):
    app.register_error_handler(Exception, lambda e: ({"error": str(e)}, 500))
    lw = make(app, engine)
    app.add_url_rule("/x", "x", lambda: 1 / 0)
    assert app.test_client().get("/x").status_code == 500
    (exc,) = entries(lw, "exception")
    assert exc.content["handled"] is True and exc.content["class"] == "builtins.ZeroDivisionError"
    assert entries(lw, "request")[0].content["status"] == 500
    lw.close()


def test_handled_4xx_not_captured(app, engine):
    app.register_error_handler(ValueError, lambda e: ({"error": "bad"}, 422))
    lw = make(app, engine)
    app.add_url_rule("/x", "x", lambda: int("nope"))
    assert app.test_client().get("/x").status_code == 422
    assert entries(lw, "exception") == []
    lw.close()


def test_unhandled_exception_not_duplicated(client, lw):
    client.get("/boom")
    assert len(entries(lw, "exception")) == 1


def test_sample_rate_zero_records_nothing(app, engine):
    lw = make(app, engine, sample_rate=0)
    app.add_url_rule("/ok", "ok", lambda: "ok")
    app.test_client().get("/ok")
    assert entries(lw) == []
    lw.close()


def test_only_errors(app, engine):
    lw = make(app, engine, only_errors=True, sample_rate=0)  # errors ignore sampling
    app.add_url_rule("/ok", "ok", lambda: "ok")
    app.add_url_rule("/bad", "bad", lambda: ("no", 503))
    c = app.test_client()
    c.get("/ok")
    assert entries(lw) == []
    c.get("/bad")
    assert [e.content["status"] for e in entries(lw, "request")] == [503]
    app.logger.error("standalone noise")
    assert entries(lw, "log") == []
    lw.close()


def test_storage_down_never_breaks_requests(app, engine):
    lw = make(app, engine)
    app.add_url_rule("/ok", "ok", lambda: "ok")

    def boom(*a, **k):
        raise RuntimeError("db down")

    lw.storage.get_setting = boom
    lw.storage.save = boom
    lw._recording = (True, 0.0)  # force a refresh
    assert app.test_client().get("/ok").status_code == 200
    lw.close()


def test_writer_drops_when_full_and_flush_times_out():
    gate = threading.Event()
    saved = []
    writer = Writer(lambda entries: (gate.wait(5), saved.append(entries)), maxsize=1)
    batch = [Entry(type="log", content={}, batch_id="b")]
    results = [writer.put(batch) for _ in range(4)]
    assert results.count(False) >= 2 and writer.dropped >= 2
    assert writer.flush(timeout=0.1) is False
    gate.set()
    assert writer.flush(timeout=5) is True


def test_writer_with_prune_disabled_still_writes():
    # AUTO_PRUNE=False passes a prune callback with interval 0: the thread must not crash.
    saved = []
    writer = Writer(saved.append, prune=lambda: None, prune_interval=0)
    writer.put([Entry(type="log", content={}, batch_id="b")])
    assert writer.flush(timeout=2) is True
    assert len(saved) == 1


def test_writer_auto_prune():
    calls = []
    writer = Writer(lambda e: None, prune=lambda: calls.append(1), prune_interval=0.2)
    writer.put([Entry(type="log", content={}, batch_id="b")])
    deadline = time.monotonic() + 3
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls


def test_chunked_prune(tmp_path, monkeypatch):
    from leeuwenhoek.storage import sqlalchemy as mod

    monkeypatch.setattr(mod, "PRUNE_CHUNK", 3)
    storage = SQLAlchemyStorage(f"sqlite:///{tmp_path}/p.db")
    storage.install()
    old = utcnow() - timedelta(hours=5)
    storage.save(
        [Entry(type="log", content={}, batch_id="b", created_at=old, tags=["a"]) for _ in range(8)]
    )
    storage.save([Entry(type="log", content={}, batch_id="b")])
    assert storage.prune(1) == 8
    assert len(storage.list()) == 1


PG = os.environ.get("LEEUWENHOEK_TEST_PG_URI")


@pytest.mark.skipif(not PG, reason="set LEEUWENHOEK_TEST_PG_URI to run against Postgres")
def test_postgres_roundtrip():
    storage = SQLAlchemyStorage(PG)
    storage.install()
    storage.install()  # idempotent
    storage.clear()
    storage.save(
        [
            Entry(
                type="request",
                content={"body": {"order": {"imported_id": "ZZ-1"}}},
                batch_id="b1",
                is_root=True,
                tags=["imported_id:ZZ-1"],
            ),
            Entry(
                type="query", content={"sql": "select 1"}, batch_id="b1", tags=["imported_id:ZZ-1"]
            ),
        ]
    )
    assert len(storage.list(tag="imported_id:ZZ-1")) == 2
    assert len(storage.list("request", q="zz-1")) == 1
    assert storage.counts() == {"request": 1, "query": 1, "batches": 1}
    storage.set_setting("recording", "0")
    storage.set_setting("recording", "1")
    assert storage.get_setting("recording") == "1"
    assert storage.prune(0) == 2
    storage.close()
