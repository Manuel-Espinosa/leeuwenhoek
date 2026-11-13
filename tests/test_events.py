import blinker
import pytest
from conftest import entries
from flask import (
    flash,
    message_flashed,
    render_template_string,
    request_started,
)

from leeuwenhoek import Leeuwenhoek

order_placed = blinker.signal("leeuwenhoek-test-order-placed")


class Shop:
    pass


@pytest.fixture
def app(app):
    app.secret_key = "test"  # flash() needs a session
    return app


@pytest.fixture
def lw(app):
    ext = Leeuwenhoek(app)
    ext.watch_signal(order_placed)

    @app.post("/order")
    def order():
        order_placed.send(Shop(), order_id=7, card={"number": "4242", "token": "tok_secret"})
        return "ok"

    @app.get("/flash")
    def flashing():
        flash("Saved", "success")
        return "ok"

    yield ext
    ext.close()


def test_custom_signal_in_request_joins_batch(app, lw):
    app.test_client().post("/order")
    (event,) = entries(lw, "event")
    (req,) = entries(lw, "request")
    assert event.batch_id == req.batch_id
    assert not event.is_root
    c = event.content
    assert c["signal"] == "leeuwenhoek-test-order-placed"
    assert c["sender_type"] == "test_events.Shop"
    assert c["sender"].startswith("<test_events.Shop object")
    assert c["kwargs"] == {"order_id": 7, "card": {"number": "4242", "token": "********"}}
    assert "event:leeuwenhoek-test-order-placed" in event.tags


def test_custom_signal_outside_request_is_standalone(app, lw):
    order_placed.send(Shop, order_id=1)
    (event,) = entries(lw, "event")
    assert event.is_root
    assert lw.storage.batch(event.batch_id) == [event]
    assert event.content["sender"] == "test_events.Shop"


def test_watch_signal_before_init_app_and_idempotent(app):
    ext = Leeuwenhoek()
    anonymous = blinker.Signal()
    assert ext.watch_signal(anonymous, name="anon") is anonymous
    ext.watch_signal(anonymous)
    before = len(anonymous.receivers)
    ext.init_app(app)
    assert len(anonymous.receivers) == before + 1
    ext.watch_signal(anonymous)
    assert len(anonymous.receivers) == before + 1
    named = ext.watch_signal("leeuwenhoek-test-by-name")
    assert named is blinker.signal("leeuwenhoek-test-by-name")
    anonymous.send(None, value=1)
    named.send(None)
    ext.close()
    assert [e.content["signal"] for e in entries(ext, "event")] == [
        "leeuwenhoek-test-by-name",
        "anon",
    ]
    assert entries(ext, "event")[1].content["sender"] is None


def test_default_flask_signal_message_flashed(app, lw):
    app.test_client().get("/flash")
    (event,) = entries(lw, "event")
    assert event.batch_id == entries(lw, "request")[0].batch_id
    assert event.content["signal"] == "message_flashed"
    assert event.content["sender_type"] == "flask.app.Flask"
    assert event.content["kwargs"] == {"message": "Saved", "category": "success"}


def test_signals_covered_by_other_watchers_not_recorded_twice(app):
    app.config["LEEUWENHOEK_SIGNALS"] = ["template_rendered", "got_request_exception"]
    ext = Leeuwenhoek(app)

    @app.get("/render")
    def render():
        return render_template_string("{{ x }}", x=1)

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    client = app.test_client()
    client.get("/render")
    client.get("/boom")
    client.get("/leeuwenhoek/requests")  # the panel renders templates too
    ext.close()
    assert entries(ext, "event") == []
    assert len(entries(ext, "template")) == 1
    assert len(entries(ext, "exception")) == 1


def test_covered_signals_recorded_when_their_watcher_is_off(app):
    app.config["LEEUWENHOEK_WATCHERS"] = ["request", "event"]
    app.config["LEEUWENHOEK_SIGNALS"] = ["template_rendered", "got_request_exception"]
    ext = Leeuwenhoek(app)

    @app.get("/render")
    def render():
        return render_template_string("{{ x }}", x=1)

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    client = app.test_client()
    client.get("/render")
    client.get("/boom")
    client.get("/leeuwenhoek/events")  # the panel's own renders are never recorded
    ext.close()
    signals = sorted(e.content["signal"] for e in entries(ext, "event"))
    assert signals == ["got_request_exception", "template_rendered"]
    failed = next(
        e for e in entries(ext, "event") if e.content["signal"] == "got_request_exception"
    )
    assert "kaboom" in failed.content["kwargs"]["exception"]


def test_signals_before_the_batch_skip_the_panel(app):
    app.config["LEEUWENHOEK_SIGNALS"] = ["request_started"]
    ext = Leeuwenhoek(app)

    @app.get("/ping")
    def ping():
        return "ok"

    client = app.test_client()
    client.get("/leeuwenhoek/events")
    client.get("/static/x.css")
    assert entries(ext, "event") == []
    client.get("/ping")
    ext.close()
    (event,) = entries(ext, "event")
    assert event.is_root  # sent before the request batch opens
    assert event.content["signal"] == "request_started"


def test_signals_from_env_and_unknown_names(app, monkeypatch, caplog):
    monkeypatch.setenv("LEEUWENHOEK_SIGNALS", "request_started, no_such_signal")
    before = len(request_started.receivers)
    ext = Leeuwenhoek(app)
    assert len(request_started.receivers) == before + 1
    assert "Unknown Flask signal: no_such_signal" in caplog.text
    ext.close()


def test_payload_truncated_and_unrepresentable_values(app):
    app.config["LEEUWENHOEK_MAX_BODY_KB"] = 1

    class Broken:
        def __repr__(self):
            raise ValueError("no repr")

    ext = Leeuwenhoek(app)
    ext.watch_signal(order_placed)
    order_placed.send(Broken(), blob="x" * 5000)
    order_placed.send(None, thing=Broken())
    ext.close()
    big, broken = sorted(entries(ext, "event"), key=lambda e: e.id or 0)
    assert len(big.content["kwargs"]) < 1200
    assert big.content["kwargs"].endswith(" characters]")
    assert big.content["sender"].startswith("<test_events.") and "Broken" in big.content["sender"]
    assert broken.content["kwargs"]["thing"].endswith(".Broken>")


def test_other_apps_signals_not_recorded(app, lw):
    from flask import Flask

    other = Flask("other")
    with other.app_context():
        order_placed.send(None)
    assert entries(lw, "event") == []


def test_listing_detail_and_related_card(app, lw):
    client = app.test_client()
    client.post("/order")
    listing = client.get("/leeuwenhoek/events")
    assert listing.status_code == 200 and "leeuwenhoek-test-order-placed" in listing.text
    event = entries(lw, "event")[0]
    detail = client.get(f"/leeuwenhoek/events/{event.uuid}").text
    assert "test_events.Shop" in detail and "order_id" in detail and "tok_secret" not in detail
    req = entries(lw, "request")[0]
    page = client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    assert 'card-header-title">Events ' in page
    assert "Events" in client.get("/leeuwenhoek/requests").text


def test_unregister_disconnects_every_signal(app):
    custom = blinker.Signal()
    counts = {s: len(s.receivers) for s in (custom, message_flashed, request_started)}
    app.config["LEEUWENHOEK_SIGNALS"] = ["message_flashed", "request_started"]
    ext = Leeuwenhoek(app)
    ext.watch_signal(custom)
    for signal, before in counts.items():
        assert len(signal.receivers) == before + 1
    ext.close()
    for signal, before in counts.items():
        assert len(signal.receivers) == before
    custom.send(None)
    assert entries(ext, "event") == []
