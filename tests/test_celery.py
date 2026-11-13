"""Celery tasks: one ``job`` batch per run, linked to the batch that dispatched it."""

import logging

import pytest
from celery import Celery, signals
from celery.contrib.testing.worker import start_worker
from conftest import entries
from sqlalchemy import text

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers.celery import BATCH_HEADER, PARENT_HEADER, SIGNALS


def noop(*args, **kwargs):
    return None


@pytest.fixture
def celery_app():
    celery = Celery("tests", broker="memory://", backend="cache+memory://")
    celery.conf.update(task_always_eager=True, task_eager_propagates=False)
    return celery


@pytest.fixture
def ext(app, engine, celery_app):
    ext = Leeuwenhoek(app, engine=engine, celery=celery_app)

    @celery_app.task(shared=False, name="tests.add")
    def add(x, y, token=None):
        logging.getLogger("app").warning("adding %s", x)
        with engine.connect() as conn:
            conn.execute(text("select * from t"))
        return x + y

    @celery_app.task(shared=False, name="tests.fail")
    def fail():
        raise ValueError("task boom")

    @celery_app.task(shared=False, name="tests.flaky", bind=True, max_retries=2)
    def flaky(self):
        if self.request.retries < 1:
            raise self.retry(countdown=0, exc=RuntimeError("try again"))
        return "ok"

    @app.post("/dispatch")
    def dispatch():
        add.delay(2, 3, token="secret")
        return "queued"

    @app.post("/dispatch-failing")
    def dispatch_failing():
        fail.delay()
        return "queued"

    ext.tasks = {"add": add, "fail": fail, "flaky": flaky}
    yield ext
    ext.close()


def runs(lw):
    return [e for e in entries(lw, "job") if e.is_root]


def dispatches(lw):
    return [e for e in entries(lw, "job") if not e.is_root]


def test_request_dispatches_a_task_in_eager_mode(app, ext):
    assert app.test_client().post("/dispatch").status_code == 200
    [request] = entries(ext, "request")
    [job] = runs(ext)
    # every entry of the job's batch is linked to the request that dispatched it
    job_batch = ext.storage.batch(job.batch_id)
    assert sorted(e.type for e in job_batch) == ["job", "log", "query"]
    assert {e.parent_batch_id for e in job_batch} == {request.batch_id}
    assert job.batch_id != request.batch_id

    c = job.content
    assert c["name"] == "tests.add"
    assert c["status"] == "SUCCESS"
    assert c["args"] == [2, 3]
    assert c["kwargs"] == {"token": "********"}
    assert c["args_redacted"] is True
    assert c["queue"] == "celery"
    assert c["attempt"] == 1 and c["retries"] == 0
    assert c["eager"] is True
    assert c["result"] == 5
    assert c["exception"] is None
    assert c["duration_ms"] >= 0
    assert {"task:tests.add", "state:SUCCESS"} <= set(job.tags)

    # the request's batch records the dispatch, pointing at the job's batch
    [dispatch] = dispatches(ext)
    assert dispatch.batch_id == request.batch_id
    assert dispatch.content["job_batch_id"] == job.batch_id
    assert dispatch.content["task_id"] == c["task_id"]

    client = app.test_client()
    detail = client.get(f"/leeuwenhoek/requests/{request.uuid}").get_data(as_text=True)
    assert "Jobs" in detail and f"/leeuwenhoek/batch/{job.batch_id}" in detail
    batch = client.get(f"/leeuwenhoek/batch/{request.batch_id}").get_data(as_text=True)
    assert "/dispatch" in batch  # the request stays on top of its batch
    job_page = client.get(f"/leeuwenhoek/batch/{job.batch_id}").get_data(as_text=True)
    assert "tests.add" in job_page and f"/leeuwenhoek/batch/{request.batch_id}" in job_page
    listing = client.get("/leeuwenhoek/jobs").get_data(as_text=True)
    assert listing.count("<code>tests.add</code>") == 1  # the run, not the dispatch
    assert client.get(f"/leeuwenhoek/jobs/{job.uuid}").status_code == 200
    assert client.get(f"/leeuwenhoek/jobs/{dispatch.uuid}").status_code == 200
    menu = client.get("/leeuwenhoek/requests").get_data(as_text=True)
    assert 'href="/leeuwenhoek/jobs"' in menu


def test_failure_is_captured_in_the_job_batch(app, ext):
    app.test_client().post("/dispatch-failing")
    [request] = entries(ext, "request")
    [job] = runs(ext)
    [exc] = entries(ext, "exception")
    assert exc.batch_id == job.batch_id
    assert exc.parent_batch_id == request.batch_id
    assert job.content["status"] == "FAILURE"
    assert job.content["exception"] == {"class": "builtins.ValueError", "message": "task boom"}
    assert job.content["args"] == [] and job.content["args_redacted"] is False


def test_replayable_arguments_are_stored_as_is(ext):
    ext.tasks["add"].delay(1, 2)
    [job] = runs(ext)
    assert job.content["args"] == [1, 2] and job.content["kwargs"] == {}
    assert job.content["args_redacted"] is False
    assert job.parent_batch_id is None  # dispatched outside any batch
    assert dispatches(ext) == []


def test_unserializable_and_truncated_arguments_are_flagged(app, engine, celery_app):
    app.config["LEEUWENHOEK_MAX_BODY_KB"] = 1
    ext = Leeuwenhoek(app, celery=celery_app)
    task = celery_app.task(shared=False, name="tests.echo")(noop)
    task.apply(args=(object(),))
    task.delay("x" * 5000)
    first, second = sorted(runs(ext), key=lambda e: e.id)
    assert first.content["args_redacted"] is True
    assert second.content["args_redacted"] is True
    assert "truncated" in second.content["args"]
    ext.close()


def test_eager_retries_are_attempts_of_the_same_dispatch(app, ext):
    with ext.batch("command") as batch:
        ext.tasks["flaky"].delay()
    attempts = sorted(runs(ext), key=lambda e: e.id)
    assert [a.content["status"] for a in attempts] == ["RETRY", "SUCCESS"]
    assert [a.content["attempt"] for a in attempts] == [1, 2]
    assert attempts[0].content["retry_reason"] == "try again"
    assert {a.parent_batch_id for a in attempts} == {batch.id}
    assert len(dispatches(ext)) == 1


def test_tasks_of_other_celery_apps_are_ignored(ext):
    other = Celery("other")
    other.conf.task_always_eager = True
    other.task(shared=False, name="other.noop")(noop).delay()
    assert runs(ext) == []


def test_without_celery_argument_every_app_is_recorded(app):
    ext = Leeuwenhoek(app)
    other = Celery("other")
    other.conf.task_always_eager = True
    other.task(shared=False, name="other.noop")(noop).delay()
    assert [e.content["name"] for e in runs(ext)] == ["other.noop"]
    ext.close()


def test_only_errors_keeps_failing_jobs(app, engine, celery_app):
    app.config["LEEUWENHOEK_ONLY_ERRORS"] = True
    ext = Leeuwenhoek(app, celery=celery_app)
    celery_app.task(shared=False, name="tests.ok")(noop).delay()

    def bad():
        raise RuntimeError("boom")

    celery_app.task(shared=False, name="tests.bad")(bad).delay()
    assert [e.content["name"] for e in runs(ext)] == ["tests.bad"]
    ext.close()


def test_disabled(app, celery_app):
    app.config["LEEUWENHOEK_ENABLED"] = False
    ext = Leeuwenhoek(app, celery=celery_app)
    celery_app.task(shared=False, name="tests.ok")(noop).delay()
    assert entries(ext) == []
    ext.close()


def _ours(signal, ext):
    uid = ext.watchers["celery"]._uid
    return [key for key, _ in signal.receivers if key[0] == uid]


def test_unregister_disconnects_every_signal(app, celery_app):
    ext = Leeuwenhoek(app, celery=celery_app)
    connected = [getattr(signals, name) for name in SIGNALS]
    assert all(_ours(signal, ext) for signal in connected)
    ext.close()
    assert not any(_ours(signal, ext) for signal in connected)
    celery_app.task(shared=False, name="tests.ok")(noop).delay()
    assert entries(ext) == []


def test_worker_links_the_job_to_the_dispatching_request(app, engine):
    """A real (in-process) worker: the batch id travels in the message headers."""
    celery = Celery("worker", broker="memory://", backend="cache+memory://")
    ext = Leeuwenhoek(app, engine=engine, celery=celery)
    seen = {}

    @celery.task(shared=False, name="worker.add")
    def add(x, y):
        from celery import current_task

        seen["parent"] = getattr(current_task.request, PARENT_HEADER, None)
        seen["batch"] = getattr(current_task.request, BATCH_HEADER, None)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return x + y

    results = []

    @app.post("/dispatch")
    def view():
        results.append(add.apply_async((1, 2), queue="celery"))
        return "queued"

    # A receiver of ``setup_logging`` keeps the worker from reconfiguring logging for the
    # rest of the test session.
    signals.setup_logging.connect(noop, weak=False, dispatch_uid="tests-no-logging")
    try:
        with start_worker(celery, perform_ping_check=False, pool="solo"):
            app.test_client().post("/dispatch")
            assert results[0].get(timeout=10) == 3
    finally:
        signals.setup_logging.disconnect(dispatch_uid="tests-no-logging")
    ext.flush()
    [request] = entries(ext, "request")
    [dispatch] = dispatches(ext)
    [job] = runs(ext)
    assert seen == {"parent": request.batch_id, "batch": job.batch_id}
    assert dispatch.content["job_batch_id"] == job.batch_id
    assert dispatch.content["queue"] == "celery"
    assert job.content["eager"] is False and job.content["status"] == "SUCCESS"
    assert {e.parent_batch_id for e in ext.storage.batch(job.batch_id)} == {request.batch_id}
    assert {e.type for e in ext.storage.batch(job.batch_id)} == {"job", "query"}
    ext.close()
