"""RQ jobs: one batch per execution, linked to the batch that enqueued the job."""

import datetime
import logging
import os
import sys

import pytest
from conftest import entries
from fakeredis import FakeRedis
from flask import Flask
from rq import Queue, SimpleWorker, Worker
from rq.job import Job
from sqlalchemy import text

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers.rq import PARENT_KEY, RQWatcher

#: ``BaseWorker`` in RQ 2 and later, ``Worker`` before
BaseWorker = next(c for c in Worker.__mro__ if "work" in c.__dict__)
ENGINE = None  # set by the ``jobs`` fixture: the job functions are imported by path


def add(a, b, token=None):
    logging.getLogger("jobs").warning("adding %s and %s", a, b)
    with ENGINE.connect() as conn:
        conn.execute(text("select * from t"))
    return a + b


def explode():
    raise ValueError("job exploded")


@pytest.fixture
def jobs(engine, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ENGINE", engine)


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
def sync_queue(redis, jobs):
    return Queue("default", connection=redis, is_async=False)


@pytest.fixture
def queue(redis, jobs):
    return Queue("default", connection=redis)


def rq_entries(lw):
    return entries(lw, "rq")


def run_burst(queue):
    SimpleWorker([queue], connection=queue.connection).work(burst=True)


def test_job_enqueued_in_a_request_is_linked_to_it(app, lw, queue):
    @app.post("/dispatch")
    def dispatch():
        job = queue.enqueue(add, 1, 2, token="abc")
        return {"id": job.id}

    job_id = app.test_client().post("/dispatch").get_json()["id"]
    [request] = entries(lw, "request")
    [dispatched] = rq_entries(lw)
    # The dispatch joins the request's batch, so the request detail lists the job.
    assert dispatched.batch_id == request.batch_id and not dispatched.is_root
    assert dispatched.content["status"] == "queued"
    assert Job.fetch(job_id, connection=queue.connection).meta[PARENT_KEY] == request.batch_id

    run_burst(queue)
    executed = next(e for e in rq_entries(lw) if e.content["event"] == "perform")
    assert executed.parent_batch_id == request.batch_id
    assert executed.batch_id != request.batch_id
    batch = lw.storage.batch(executed.batch_id)
    assert sorted(e.type for e in batch) == ["log", "query", "rq"]
    assert all(e.parent_batch_id == request.batch_id for e in batch)
    # The worker's own logs and bookkeeping, outside the job, are not recorded.
    assert [e.batch_id for e in entries(lw, "log")] == [executed.batch_id]
    c = executed.content
    assert c["job_id"] == job_id
    assert c["function"] == "test_rq.add"
    assert c["queue"] == "default"
    assert c["status"] == "finished"
    assert c["args"] == [1, 2]
    assert c["kwargs"] == {"token": "********"}
    assert c["args_redacted"] is True
    assert c["duration_ms"] >= 0
    assert "abc" not in str(c)
    assert {f"rq_job:{job_id}", "rq_queue:default", "rq_status:finished"} <= set(executed.tags)

    # The request detail links to the job; the job detail links back to the request's batch.
    client = app.test_client()
    page = client.get(f"/leeuwenhoek/requests/{request.uuid}").get_data(as_text=True)
    assert "RQ jobs" in page and "test_rq.add" in page
    page = client.get(f"/leeuwenhoek/rq/{executed.uuid}").get_data(as_text=True)
    assert f"/leeuwenhoek/batch/{request.batch_id}" in page
    assert f"rq_job:{job_id}" in page
    batch_page = client.get(f"/leeuwenhoek/batch/{request.batch_id}").get_data(as_text=True)
    assert "/dispatch" in batch_page  # the request stays on top, not the dispatched job
    listing = client.get("/leeuwenhoek/rq").get_data(as_text=True)
    assert "finished" in listing and "queued" in listing


def test_failing_job_records_the_exception_in_its_batch(app, lw, queue):
    job = queue.enqueue(explode)
    run_burst(queue)
    [executed] = [e for e in rq_entries(lw) if e.content["event"] == "perform"]
    assert executed.content["status"] == "failed"
    assert executed.content["error"] == {"class": "builtins.ValueError", "message": "job exploded"}
    assert "rq_status:failed" in executed.tags
    [exc] = entries(lw, "exception")
    assert exc.batch_id == executed.batch_id
    assert exc.content["message"] == "job exploded"
    assert Job.fetch(job.id, connection=queue.connection).get_status() == "failed"
    # Enqueued outside any batch: the dispatch is standalone and the job has no parent.
    [dispatched] = [e for e in rq_entries(lw) if e.content["event"] == "enqueue"]
    assert dispatched.batch_id != executed.batch_id
    assert executed.parent_batch_id is None


def test_sync_queue_inside_a_request(app, lw, sync_queue):
    @app.get("/sync")
    def sync():
        job = sync_queue.enqueue(add, 2, 3)
        return {"result": job.return_value()}

    assert app.test_client().get("/sync").get_json() == {"result": 5}
    [request] = entries(lw, "request")
    [executed] = [e for e in rq_entries(lw) if e.content["event"] == "perform"]
    [dispatched] = [e for e in rq_entries(lw) if e.content["event"] == "enqueue"]
    assert executed.parent_batch_id == request.batch_id
    assert dispatched.batch_id == request.batch_id
    assert dispatched.content["status"] == "finished"
    # Nothing of the job leaks into the request's batch.
    assert sorted(e.type for e in lw.storage.batch(request.batch_id)) == ["request", "rq"]


def test_args_are_kept_for_a_retry_only_when_nothing_was_redacted(app, lw, sync_queue):
    sync_queue.enqueue(add, 1, b=2)
    sync_queue.enqueue(add, datetime.date(2026, 1, 1), 1)  # not plain JSON
    clean, odd = [e.content for e in reversed(rq_entries(lw)) if e.content["event"] == "perform"]
    assert clean["args"] == [1] and clean["kwargs"] == {"b": 2}
    assert clean["args_redacted"] is False
    assert odd["args_redacted"] is True


def test_payloads_are_truncated(app, lw, sync_queue):
    app.config["LEEUWENHOEK_MAX_BODY_KB"] = 1
    lw.config = type(lw.config)(app)
    sync_queue.enqueue(add, "x" * 4000, "y")
    [executed] = [e for e in rq_entries(lw) if e.content["event"] == "perform"]
    assert "truncated" in executed.content["args"]
    assert executed.content["args_redacted"] is True


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_forked_work_horse_writes_before_os_exit(app, tmp_path, queue):
    """The horse ends with ``os._exit()``: the job's batch must be written before it."""
    app.config.update(
        LEEUWENHOEK_STORAGE="sqlalchemy",
        LEEUWENHOEK_DATABASE_URI=f"sqlite:///{tmp_path / 'lw.db'}",
        LEEUWENHOEK_ASYNC_WRITE=True,
        LEEUWENHOEK_WATCHERS=["rq", "exception"],
    )
    ext = Leeuwenhoek(app)
    try:
        job = queue.enqueue(explode)
        ext.flush()
        pid = os.fork()
        if pid == 0:  # the work horse
            try:
                job.perform()
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        types = sorted(e.type for e in ext.storage.list(None, limit=100))
        assert types == ["exception", "rq", "rq"]
    finally:
        ext.close()


def test_unregister_restores_rq(app, engine):
    def methods():
        return (Queue.create_job, Queue.enqueue_job, Job.perform, BaseWorker.work)

    originals = methods()
    ext = Leeuwenhoek(app, engine=engine)
    assert isinstance(ext.watchers["rq"], RQWatcher)
    assert all(new is not old for new, old in zip(methods(), originals))
    ext.close()
    assert methods() == originals


def test_unregister_keeps_wrappers_chained_on_top(app, engine, sync_queue, monkeypatch):
    original = Queue.create_job
    patched = (
        (Queue, "create_job"),
        (Queue, "enqueue_job"),
        (Job, "perform"),
        (BaseWorker, "work"),
    )
    for cls, attr in patched:
        monkeypatch.setattr(cls, attr, getattr(cls, attr))  # drop the leftover wrappers after
    first = Leeuwenhoek(app, engine=engine)
    other = Flask("other")
    other.config.update(app.config)
    second = Leeuwenhoek(other, engine=engine)
    first.close()  # ours is not on top: it stays in the chain and only delegates
    sync_queue.enqueue(add, 1, 1)
    assert [e.content["event"] for e in rq_entries(second)] == ["enqueue", "perform"]
    assert rq_entries(first) == []
    second.close()
    assert Queue.create_job is not original  # the first one's wrapper still delegates


def test_disabled(app, engine, sync_queue):
    app.config["LEEUWENHOEK_ENABLED"] = False
    ext = Leeuwenhoek(app, engine=engine)
    job = sync_queue.enqueue(add, 1, 1)
    assert job.return_value() == 2
    assert rq_entries(ext) == []
    ext.close()


def test_not_listed_when_disabled_by_config(app, engine):
    app.config["LEEUWENHOEK_WATCHERS"] = ["request"]
    ext = Leeuwenhoek(app, engine=engine)
    assert "rq" not in ext.watchers
    assert app.test_client().get("/leeuwenhoek/rq").status_code == 404
    ext.close()
