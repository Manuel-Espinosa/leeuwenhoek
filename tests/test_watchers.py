from conftest import entries
from sqlalchemy import text


def test_request_batch_groups_everything(client, lw):
    client.get("/hello?token=abc&x=1", headers={"Authorization": "Bearer s3cret"})
    rows = entries(lw)
    assert {e.type for e in rows} == {"request", "query", "log"}
    assert len({e.batch_id for e in rows}) == 1
    req = entries(lw, "request")[0]
    assert req.is_root and req.content["status"] == 200
    assert req.content["endpoint"] == "hello"
    assert "endpoint:hello" in req.tags and "status:200" in req.tags
    assert req.content["response_body"]


def test_redaction(client, lw):
    client.post(
        "/login?token=abc",
        json={"user": "a", "password": "hunter2", "nested": {"secret": "x"}},
        headers={"Authorization": "Bearer s3cret", "X-Api-Key": "k"},
    )
    c = entries(lw, "request")[0].content
    assert c["headers"]["Authorization"] == "********"
    assert c["headers"]["X-Api-Key"] == "********"
    assert c["query"]["token"] == "********"
    assert c["body"]["password"] == "********"
    assert c["body"]["nested"]["secret"] == "********"
    assert c["body"]["user"] == "a"


def test_exception_family_and_status(client, lw):
    client.get("/boom")
    client.get("/boom")
    excs = entries(lw, "exception")
    assert len(excs) == 2
    assert excs[0].family_hash == excs[1].family_hash
    assert excs[0].content["class"] == "builtins.RuntimeError"
    assert excs[0].content["frames"][-1]["snippet"]
    assert entries(lw, "request")[0].content["status"] == 500


def test_query_captured_with_origin(client, lw):
    client.get("/hello")
    q = entries(lw, "query")[0].content
    assert "select * from t" in q["sql"]
    assert q["interpolated"].endswith("id = 1")
    assert q["file"].endswith("conftest.py")


def test_lw_own_queries_not_recorded(client, lw):
    client.get("/hello")
    client.get("/leeuwenhoek/requests")
    assert all("leeuwenhoek_" not in e.content["sql"] for e in entries(lw, "query"))


def test_ignored_and_panel_paths(app, lw, client):
    app.add_url_rule("/health/live", "live", lambda: "ok")
    client.get("/health/live")
    client.get("/leeuwenhoek/requests")
    client.get("/static/x.css")
    assert entries(lw, "request") == []


def test_dump(app, lw, client):
    from leeuwenhoek import dump

    @app.get("/d")
    def d():
        dump({"hello": "world"})
        return "ok"

    client.get("/d")
    dumped = entries(lw, "dump")[0]
    assert "'hello': 'world'" in dumped.content["repr"]
    assert dumped.batch_id == entries(lw, "request")[0].batch_id


def test_standalone_log(app, lw):
    app.logger.error("outside a request")
    log = entries(lw, "log")[0]
    assert log.is_root and log.content["message"] == "outside a request"


def test_pause_recording(client, lw):
    lw.set_recording(False)
    client.get("/hello")
    assert entries(lw) == []
    lw.set_recording(True)
    client.get("/hello")
    assert entries(lw, "request")


def test_max_entries_per_batch(app, engine):
    from leeuwenhoek import Leeuwenhoek

    app.config["LEEUWENHOEK_MAX_ENTRIES_PER_BATCH"] = 2
    ext = Leeuwenhoek(app, engine=engine)
    app.add_url_rule("/many", "many", lambda: [app.logger.warning("x") for _ in range(10)] and "ok")
    app.test_client().get("/many")
    assert len(entries(ext)) == 3  # 2 logs + the forced request entry
    ext.close()


class FakeClock:
    """Stand-in for the ``time`` module whose ``perf_counter`` advances ``step`` seconds per call."""

    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def perf_counter(self) -> float:
        self.now += self.step
        return self.now


SLOW_TAG = '<span class="tag is-warning">slow</span>'


def test_query_over_threshold_is_slow(monkeypatch, client, lw):
    from leeuwenhoek.watchers import query

    monkeypatch.setattr(query, "time", FakeClock(0.2))  # every query takes 200 ms
    client.get("/hello")
    q = entries(lw, "query")[0]
    assert q.content["duration_ms"] == 200 and q.content["slow"] is True
    assert "slow" in q.tags

    assert SLOW_TAG in client.get("/leeuwenhoek/queries").text
    assert SLOW_TAG in client.get(f"/leeuwenhoek/queries/{q.uuid}").text
    req = entries(lw, "request")[0]
    assert "1 slow</span>" in client.get(f"/leeuwenhoek/requests/{req.uuid}").text


def test_query_under_threshold_is_not_slow(monkeypatch, app, engine):
    from leeuwenhoek import Leeuwenhoek
    from leeuwenhoek.watchers import query

    app.config["LEEUWENHOEK_SLOW_QUERY_MS"] = 500
    ext = Leeuwenhoek(app, engine=engine)
    monkeypatch.setattr(query, "time", FakeClock(0.2))

    @app.get("/q")
    def q_view():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return "ok"

    client = app.test_client()
    client.get("/q")
    q = entries(ext, "query")[0]
    assert q.content["duration_ms"] == 200 and q.content["slow"] is False
    assert "slow" not in q.tags

    assert SLOW_TAG not in client.get("/leeuwenhoek/queries").text
    req = entries(ext, "request")[0]
    assert "slow</span>" not in client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    ext.close()
