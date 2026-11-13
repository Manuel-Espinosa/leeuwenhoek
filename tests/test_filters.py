import logging

import pytest
from conftest import entries

from leeuwenhoek import Leeuwenhoek


@pytest.fixture
def filtered(app, engine):
    lw = Leeuwenhoek(app, engine=engine)

    @app.get("/hello")
    def hello():
        app.logger.warning("noisy")
        app.logger.error("important")
        return "ok"

    @app.get("/health")
    def health():
        app.logger.warning("health check")
        return "ok"

    yield lw
    lw.close()


def test_entries_are_kept_by_default_and_when_filters_pass(app, filtered):
    filtered.filter(lambda entry: True)
    app.test_client().get("/hello")
    assert len(entries(filtered, "request")) == 1
    assert len(entries(filtered, "log")) == 2


def test_filter_drops_entries(app, filtered):
    @filtered.filter
    def no_warnings(entry):
        return not (entry.type == "log" and entry.content["level"] == "WARNING")

    app.test_client().get("/hello")
    logs = entries(filtered, "log")
    assert [e.content["message"] for e in logs] == ["important"]
    assert len(entries(filtered, "request")) == 1


def test_several_filters_all_apply(app, filtered):
    filtered.filter(lambda e: e.type != "log" or e.content["message"] != "noisy")
    filtered.filter(lambda e: e.type != "log" or e.content["message"] != "important")
    app.test_client().get("/hello")
    assert entries(filtered, "log") == []
    assert len(entries(filtered, "request")) == 1


def test_dropping_the_root_entry_drops_the_batch(app, filtered):
    @filtered.filter
    def no_health(entry):
        return not (entry.type == "request" and entry.content["path"] == "/health")

    c = app.test_client()
    c.get("/health")
    assert entries(filtered) == []
    c.get("/hello")
    assert len(entries(filtered, "request")) == 1


def test_dropping_a_standalone_entry(app, filtered):
    def no_logs(entry):
        return entry.type != "log"

    assert filtered.filter(no_logs) is no_logs
    with app.app_context():
        app.logger.warning("outside a request")
        filtered._filters.clear()
        app.logger.warning("kept")
    assert [e.content["message"] for e in entries(filtered)] == ["kept"]


def test_raising_filter_keeps_the_entry(app, filtered, caplog):
    @filtered.filter
    def broken(entry):
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR, logger="leeuwenhoek"):
        app.test_client().get("/hello")
    assert len(entries(filtered, "request")) == 1
    assert len(entries(filtered, "log")) == 2
    assert "An entry filter failed" in caplog.text


def test_filters_run_before_taggers(app, filtered):
    seen = []
    filtered.tag(lambda e: seen.append(e.type) or [])
    filtered.filter(lambda e: e.type != "log")
    app.test_client().get("/hello")
    assert seen == ["request"]
