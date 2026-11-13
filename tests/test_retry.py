"""Retrying failed Celery tasks and RQ jobs from the panel (#21)."""

from urllib.parse import parse_qs, urlsplit

import pytest
from celery import Celery
from conftest import entries
from fakeredis import FakeRedis
from rq import Queue, SimpleWorker

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers.celery import RETRY_HEADER, _header

#: calls of the jobs below, and whether they fail
CALLS: list[tuple] = []
FAIL = {"on": True}


def charge(order_id, amount=0):
    CALLS.append((order_id, amount))
    if FAIL["on"]:
        raise RuntimeError("payment gateway down")
    return amount


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    CALLS.clear()
    monkeypatch.setitem(FAIL, "on", True)


def token(client):
    return client.get_cookie("lw_csrf").value


def post(client, url, csrf=True):
    return client.post(url, data={"csrf_token": token(client)} if csrf else {})


# ------------------------------------------------------------------------- Celery


@pytest.fixture
def celery_app():
    celery = Celery("tests", broker="memory://", backend="cache+memory://")
    celery.conf.update(task_always_eager=True, task_eager_propagates=False)
    celery.task(shared=False, name="tests.charge")(charge)
    return celery


@pytest.fixture
def celery_lw(app, engine, celery_app):
    ext = Leeuwenhoek(app, engine=engine, celery=celery_app)

    @app.post("/order")
    def order():
        celery_app.tasks["tests.charge"].delay(2)
        return "ok"

    client = app.test_client()
    client.get("/leeuwenhoek/jobs")  # issues the CSRF cookie
    yield ext, client
    ext.close()


def celery_runs(lw):
    return [e for e in entries(lw, "job") if e.is_root]


def failed_celery_run(lw, celery_app, *args, **kwargs):
    celery_app.tasks["tests.charge"].delay(*args, **kwargs)
    [run] = [e for e in celery_runs(lw) if e.content["status"] == "FAILURE"]
    return run


def test_celery_retry_dispatches_with_the_original_arguments(celery_lw, celery_app):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 7, amount=30)
    assert failed.content["args_redacted"] is False
    url = f"/leeuwenhoek/jobs/{failed.uuid}"

    html = client.get(url).text
    assert f'action="{url}/retry"' in html
    assert "Retry</button>" in html and "disabled" not in html

    FAIL["on"] = False
    resp = post(client, f"{url}/retry")
    assert resp.status_code == 302
    assert CALLS == [(7, 30), (7, 30)]

    [retried] = [e for e in celery_runs(lw) if e.uuid != failed.uuid]
    assert retried.content["status"] == "SUCCESS"
    assert retried.content["args"] == [7] and retried.content["kwargs"] == {"amount": 30}
    assert retried.content["retry_of"] == failed.uuid
    assert f"retry_of:{failed.uuid}" in retried.tags
    assert "retry_of" not in failed.content

    # Back on the failed run, with a notice and a link to the new run...
    assert (
        resp.location.startswith(url) and f"retried={retried.content['task_id']}" in resp.location
    )
    html = client.get(resp.location).text
    assert "Retry dispatched" in html and retried.content["task_id"] in html
    assert f"tag=retry_of:{failed.uuid}" in html
    listing = client.get(f"/leeuwenhoek/jobs?tag=retry_of:{failed.uuid}").text
    assert f"/leeuwenhoek/jobs/{retried.uuid}" in listing
    # ...and the new run links back to it. A successful run gets no Retry button.
    html = client.get(f"/leeuwenhoek/jobs/{retried.uuid}").text
    assert f'href="{url}"' in html and "Retry of" in html
    assert "/retry" not in html


def test_celery_retry_requires_csrf(celery_lw, celery_app):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 1)
    url = f"/leeuwenhoek/jobs/{failed.uuid}/retry"
    assert post(client, url, csrf=False).status_code == 400
    assert client.post(url, data={"csrf_token": "forged"}).status_code == 400
    assert CALLS == [(1, 0)]


def test_celery_redacted_arguments_are_refused(celery_lw, celery_app):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 1, amount={"token": "abc"})
    assert failed.content["args_redacted"] is True
    url = f"/leeuwenhoek/jobs/{failed.uuid}"
    html = client.get(url).text
    assert "disabled" in html and "redacted" in html

    resp = post(client, f"{url}/retry")
    assert resp.status_code == 409 and "Retry not dispatched" in resp.text
    assert len(CALLS) == 1 and len(celery_runs(lw)) == 1


def test_celery_only_failed_runs_can_be_retried(celery_lw, celery_app):
    lw, client = celery_lw
    FAIL["on"] = False
    celery_app.tasks["tests.charge"].delay(1)
    [ok] = celery_runs(lw)
    assert "/retry" not in client.get(f"/leeuwenhoek/jobs/{ok.uuid}").text
    resp = post(client, f"/leeuwenhoek/jobs/{ok.uuid}/retry")
    assert resp.status_code == 409 and "Only failed runs" in resp.text

    # The dispatch entry recorded in the dispatching batch is not a run.
    FAIL["on"] = True
    lw.app.test_client().post("/order")
    [dispatch] = [e for e in entries(lw, "job") if not e.is_root]
    assert post(client, f"/leeuwenhoek/jobs/{dispatch.uuid}/retry").status_code == 409
    assert len(CALLS) == 2


def test_retry_of_unknown_or_other_entries_is_404(celery_lw, celery_app):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 1)
    assert post(client, "/leeuwenhoek/jobs/nope/retry").status_code == 404
    assert post(client, f"/leeuwenhoek/rq/{failed.uuid}/retry").status_code == 404
    [exc] = entries(lw, "exception")
    assert post(client, f"/leeuwenhoek/exceptions/{exc.uuid}/retry").status_code == 404


def test_retry_can_be_disabled(app, celery_lw, celery_app):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 1)
    app.config["LEEUWENHOEK_ALLOW_RETRY"] = False
    lw.config = type(lw.config)(app)
    html = client.get(f"/leeuwenhoek/jobs/{failed.uuid}").text
    assert "disabled" in html and "LEEUWENHOEK_ALLOW_RETRY" in html
    assert post(client, f"/leeuwenhoek/jobs/{failed.uuid}/retry").status_code == 403
    assert len(CALLS) == 1


def test_retry_follows_panel_access(app, celery_lw, celery_app):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 1)
    lw.authorize(lambda: False)
    assert post(client, f"/leeuwenhoek/jobs/{failed.uuid}/retry").status_code == 403
    assert len(CALLS) == 1


def test_retry_is_off_by_default_outside_debug(app, celery_lw, celery_app):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 1)
    app.debug = False
    lw.authorize(lambda: True)
    assert post(client, f"/leeuwenhoek/jobs/{failed.uuid}/retry").status_code == 403
    app.config["LEEUWENHOEK_ALLOW_RETRY"] = True
    lw.config = type(lw.config)(app)
    assert post(client, f"/leeuwenhoek/jobs/{failed.uuid}/retry").status_code == 302
    assert len(CALLS) == 2


def test_celery_task_not_registered_is_sent_by_name(celery_lw, celery_app, monkeypatch):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 3, amount=4)
    sent = []

    class Result:
        id = "sent-id"

    def send_task(name, args, kwargs, **options):
        sent.append((name, args, kwargs, options))
        return Result()

    del celery_app.tasks["tests.charge"]
    monkeypatch.setattr(celery_app, "send_task", send_task)
    resp = post(client, f"/leeuwenhoek/jobs/{failed.uuid}/retry")
    assert resp.status_code == 302 and "retried=sent-id" in resp.location
    assert sent == [
        (
            "tests.charge",
            [3],
            {"amount": 4},
            {"headers": {RETRY_HEADER: failed.uuid}, "queue": "celery"},
        )
    ]


def test_celery_dispatch_error_is_reported(celery_lw, celery_app, monkeypatch):
    lw, client = celery_lw
    failed = failed_celery_run(lw, celery_app, 1)

    def broken(*args, **kwargs):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(celery_app.tasks["tests.charge"], "apply_async", broken)
    resp = post(client, f"/leeuwenhoek/jobs/{failed.uuid}/retry")
    assert resp.status_code == 502 and "broker unreachable" in resp.text


def test_retry_header_is_read_on_workers_and_eager():
    class Worker:
        leeuwenhoek_retry_of = "a"
        headers = None

    class Eager:
        def __init__(self):
            self.headers = {RETRY_HEADER: "b"}

    assert _header(Worker(), RETRY_HEADER) == "a"
    assert _header(Eager(), RETRY_HEADER) == "b"
    assert _header(object(), RETRY_HEADER) is None


# ------------------------------------------------------------------------- RQ


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
def rq_lw(app, engine, redis):
    ext = Leeuwenhoek(app, engine=engine, rq_connection=redis)
    client = app.test_client()
    client.get("/leeuwenhoek/rq")
    yield ext, client
    ext.close()


def run_burst(redis, name="default"):
    SimpleWorker([Queue(name, connection=redis)], connection=redis).work(burst=True)


def rq_runs(lw):
    return [e for e in entries(lw, "rq") if e.content["event"] == "perform"]


def failed_rq_run(lw, redis, *args, queue="default", **kwargs):
    Queue(queue, connection=redis).enqueue(charge, *args, **kwargs)
    run_burst(redis, queue)
    [run] = [e for e in rq_runs(lw) if e.content["status"] == "failed"]
    return run


def test_rq_retry_enqueues_with_the_original_arguments(rq_lw, redis):
    lw, client = rq_lw
    failed = failed_rq_run(lw, redis, 5, queue="payments", amount=12)
    assert failed.content["function"] == f"{__name__}.charge"
    assert failed.content["args_redacted"] is False
    url = f"/leeuwenhoek/rq/{failed.uuid}"
    html = client.get(url).text
    assert f'action="{url}/retry"' in html and "disabled" not in html

    resp = post(client, f"{url}/retry")
    assert resp.status_code == 302
    [job_id] = parse_qs(urlsplit(resp.location).query)["retried"]
    assert len(CALLS) == 1  # enqueued, not run yet

    FAIL["on"] = False
    run_burst(redis, "payments")
    assert CALLS == [(5, 12), (5, 12)]
    [retried] = [e for e in rq_runs(lw) if e.uuid != failed.uuid]
    c = retried.content
    assert c["job_id"] == job_id != failed.content["job_id"]
    assert c["status"] == "finished" and c["queue"] == "payments"
    assert c["args"] == [5] and c["kwargs"] == {"amount": 12}
    assert c["retry_of"] == failed.uuid and f"retry_of:{failed.uuid}" in retried.tags

    html = client.get(resp.location).text
    assert "Retry dispatched" in html and job_id in html
    assert f"tag=retry_of:{failed.uuid}" in html
    html = client.get(f"/leeuwenhoek/rq/{retried.uuid}").text
    assert f'href="{url}"' in html and "Retry of" in html and "/retry" not in html


def test_rq_retry_requires_csrf(rq_lw, redis):
    lw, client = rq_lw
    failed = failed_rq_run(lw, redis, 1)
    assert post(client, f"/leeuwenhoek/rq/{failed.uuid}/retry", csrf=False).status_code == 400
    assert Queue("default", connection=redis).count == 0


def test_rq_redacted_or_non_failed_entries_are_refused(rq_lw, redis):
    lw, client = rq_lw
    failed = failed_rq_run(lw, redis, 1, amount={"secret": "x"})
    assert failed.content["args_redacted"] is True
    html = client.get(f"/leeuwenhoek/rq/{failed.uuid}").text
    assert "disabled" in html and "redacted" in html
    assert post(client, f"/leeuwenhoek/rq/{failed.uuid}/retry").status_code == 409

    FAIL["on"] = False
    Queue("default", connection=redis).enqueue(charge, 2)
    run_burst(redis)
    [ok] = [e for e in rq_runs(lw) if e.content["status"] == "finished"]
    assert post(client, f"/leeuwenhoek/rq/{ok.uuid}/retry").status_code == 409
    [enqueue, *_] = [e for e in entries(lw, "rq") if e.content["event"] == "enqueue"]
    assert post(client, f"/leeuwenhoek/rq/{enqueue.uuid}/retry").status_code == 409
    assert Queue("default", connection=redis).count == 0


def test_rq_without_connection_is_disabled(app, engine, redis):
    lw = Leeuwenhoek(app, engine=engine)
    try:
        client = app.test_client()
        client.get("/leeuwenhoek/rq")
        failed = failed_rq_run(lw, redis, 1)
        html = client.get(f"/leeuwenhoek/rq/{failed.uuid}").text
        assert "disabled" in html and "rq_connection" in html
        assert post(client, f"/leeuwenhoek/rq/{failed.uuid}/retry").status_code == 409
    finally:
        lw.close()


def test_rq_connection_from_url(app, engine, redis, monkeypatch):
    import redis as redis_lib

    from leeuwenhoek import retry

    app.config["LEEUWENHOEK_RQ_REDIS_URL"] = "redis://example:6379/0"
    monkeypatch.setattr(redis_lib.Redis, "from_url", classmethod(lambda cls, url: (url, redis)))
    lw = Leeuwenhoek(app, engine=engine)
    try:
        assert retry.rq_connection(lw) == ("redis://example:6379/0", redis)
    finally:
        lw.close()
