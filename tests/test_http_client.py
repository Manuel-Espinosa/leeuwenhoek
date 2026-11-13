"""Outgoing HTTP watcher: requests and httpx, without touching the network."""

import asyncio

import httpx
import pytest
import requests
from conftest import entries
from flask import Flask
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.filters import redact_url
from leeuwenhoek.watchers import http_client
from leeuwenhoek.watchers.http_client import HttpClientWatcher

MASK = "********"


def fake_response(request, status=200, body=b"", headers=None):
    """A ``requests.Response`` as ``HTTPAdapter.send`` would return it, already read."""
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    response._content = body
    response._content_consumed = True
    response.url = request.url
    response.request = request
    response.reason = "OK"
    return response


@pytest.fixture
def adapter(monkeypatch):
    """Route every ``requests`` call through ``routes[url] -> response`` instead of a socket."""
    routes = {}
    sent = []

    def send(self, request, **kwargs):
        sent.append(request)
        route = routes[request.url]
        if isinstance(route, Exception):
            raise route
        return route(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return routes, sent


def json_api(request):
    return httpx.Response(
        201,
        json={"id": 7, "token": "t0k3n"},
        headers={"Set-Cookie": "sid=1", "X-Trace": "abc"},
    )


def test_requests_call_joins_the_request_batch(app, lw, client, adapter):
    routes, _ = adapter
    routes["http://api.test/items?token=abc&x=1"] = lambda r: fake_response(
        r,
        201,
        b'{"id": 7, "token": "t0k3n"}',
        {"Content-Type": "application/json", "X-Trace": "abc"},
    )

    @app.get("/call")
    def call():
        requests.post(
            "http://api.test/items?token=abc&x=1",
            json={"name": "a", "password": "hunter2"},
            headers={"Authorization": "Bearer s3cret"},
        )
        return "ok"

    client.get("/call")
    call_entry = entries(lw, "http")[0]
    req = entries(lw, "request")[0]
    assert call_entry.batch_id == req.batch_id and not call_entry.is_root
    c = call_entry.content
    assert c["library"] == "requests"
    assert c["method"] == "POST"
    assert c["url"] == f"http://api.test/items?token={MASK}&x=1"
    assert c["host"] == "api.test"
    assert c["request_headers"]["Authorization"] == MASK
    assert c["payload"] == {"name": "a", "password": MASK}
    assert c["status"] == 201
    assert c["response_headers"]["X-Trace"] == "abc"
    assert c["response_body"] == {"id": 7, "token": MASK}
    assert c["duration_ms"] >= 0 and c["error"] is None
    assert "host:api.test" in call_entry.tags


def test_requests_redirects_are_one_entry(lw, adapter):
    routes, sent = adapter
    routes["http://api.test/old"] = lambda r: fake_response(
        r, 302, headers={"Location": "http://api.test/new"}
    )
    routes["http://api.test/new"] = lambda r: fake_response(
        r, 200, b"done", {"Content-Type": "text/plain"}
    )

    requests.get("http://api.test/old")
    assert len(sent) == 2
    [entry] = entries(lw, "http")
    assert entry.content["url"] == "http://api.test/old"
    assert entry.content["final_url"] == "http://api.test/new"
    assert entry.content["redirects"] == 1
    assert entry.content["status"] == 200 and entry.content["response_body"] == "done"


def test_requests_error_is_recorded_and_raised(lw, adapter):
    routes, _ = adapter
    routes["http://down.test/"] = requests.ConnectionError("connection refused")

    with pytest.raises(requests.ConnectionError):
        requests.get("http://down.test/")
    entry = entries(lw, "http")[0]
    assert entry.content["status"] is None
    assert entry.content["error"]["class"] == "requests.exceptions.ConnectionError"
    assert "connection refused" in entry.content["error"]["message"]
    assert "failed" in entry.tags


def test_requests_stream_and_binary_bodies_are_not_read(lw, adapter):
    routes, _ = adapter
    routes["http://api.test/img"] = lambda r: fake_response(
        r, 200, b"\x89PNG", {"Content-Type": "image/png"}
    )
    routes["http://api.test/stream"] = lambda r: fake_response(
        r, 200, b"chunk", {"Content-Type": "text/plain"}
    )

    requests.post("http://api.test/img", data=b"\xff\xfe", headers={"Content-Type": "x/y"})
    requests.get("http://api.test/stream", stream=True)
    stream, img = entries(lw, "http")
    assert img.content["response_body"] is None
    assert img.content["payload"] == "[binary body: 2 bytes]"
    assert stream.content["response_body"] is None


def test_httpx_sync_in_batch_with_truncation(app, engine):
    app.config["LEEUWENHOEK_MAX_BODY_KB"] = 1
    ext = Leeuwenhoek(app, engine=engine)
    big = "x" * 3000
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=big))

    @app.get("/call")
    def call():
        with httpx.Client(transport=transport) as http:
            http.post(
                "https://api.test/upload",
                data={"secret": "s", "note": big},
                headers={"X-Api-Key": "k"},
            )
        return "ok"

    app.test_client().get("/call")
    entry = entries(ext, "http")[0]
    assert entry.batch_id == entries(ext, "request")[0].batch_id
    c = entry.content
    assert c["library"] == "httpx" and c["status"] == 200
    assert c["request_headers"]["x-api-key"] == MASK
    assert c["response_body"].endswith("… [truncated: 3000 characters]")
    assert len(c["response_body"]) < 1100
    assert "[truncated:" in c["payload"] and f'"secret": "{MASK}"' in c["payload"]
    ext.close()


def test_httpx_async_outside_a_request_is_standalone(lw):
    async def main():
        transport = httpx.MockTransport(json_api)
        async with httpx.AsyncClient(transport=transport) as http:
            return await http.get("https://api.test/me", params={"password": "p", "q": "1"})

    assert asyncio.run(main()).status_code == 201
    entry = entries(lw, "http")[0]
    assert entry.is_root  # standalone batch
    assert entry.content["url"] == f"https://api.test/me?password={MASK}&q=1"
    assert entry.content["response_body"] == {"id": 7, "token": MASK}
    assert entry.content["response_headers"]["x-trace"] == "abc"


def test_httpx_errors_sync_and_async(lw):
    def fail(request):
        raise httpx.ConnectError("no route", request=request)

    transport = httpx.MockTransport(fail)
    with pytest.raises(httpx.ConnectError), httpx.Client(transport=transport) as http:
        http.get("https://down.test/")

    async def main():
        async with httpx.AsyncClient(transport=transport) as http:
            await http.get("https://down.test/async")

    with pytest.raises(httpx.ConnectError):
        asyncio.run(main())
    errors = [e.content["error"] for e in entries(lw, "http")]
    assert [e["class"] for e in errors] == ["httpx.ConnectError"] * 2
    assert errors[0]["message"] == "no route"


def test_not_recorded_when_paused(lw, client, app):
    transport = httpx.MockTransport(json_api)
    lw.set_recording(False)
    app.add_url_rule(
        "/call", "call", lambda: httpx.Client(transport=transport).get("http://a/").text
    )
    client.get("/call")
    httpx.Client(transport=transport).get("http://a/")
    assert entries(lw, "http") == []


def test_unregister_in_any_order_restores_the_clients(app, engine):
    other = Flask("other")
    other.config.update(app.config)
    other.debug = True
    originals = (requests.Session.send, httpx.Client.send, httpx.AsyncClient.send)
    first = Leeuwenhoek(app, engine=engine)
    second = Leeuwenhoek(other, engine=engine)
    assert requests.Session.send is not originals[0]
    first.close()  # not on top: stays in the chain, only delegates
    transport = httpx.MockTransport(json_api)
    httpx.Client(transport=transport).get("http://a/")
    assert entries(first, "http") == [] and len(entries(second, "http")) == 1
    second.close()  # on top: skips first's inactive wrapper too
    assert (requests.Session.send, httpx.Client.send, httpx.AsyncClient.send) == originals


def test_unregister_in_order_leaves_no_patch(app, engine):
    originals = (requests.Session.send, httpx.Client.send, httpx.AsyncClient.send)
    ext = Leeuwenhoek(app, engine=engine)
    assert httpx.AsyncClient.send is not originals[2]
    ext.close()
    assert (requests.Session.send, httpx.Client.send, httpx.AsyncClient.send) == originals


def test_either_library_is_enough(monkeypatch, app):
    monkeypatch.setattr(http_client, "_installed", lambda m: m == "httpx")
    assert HttpClientWatcher.available() and HttpClientWatcher.missing() == []
    original = requests.Session.send
    app.config["LEEUWENHOEK_WATCHERS"] = ["http"]
    ext = Leeuwenhoek(app)
    assert requests.Session.send is original  # requests "not installed": left alone
    ext.close()

    monkeypatch.setattr(http_client, "_installed", lambda m: False)
    assert not HttpClientWatcher.available()
    assert HttpClientWatcher.missing() == ["requests", "httpx"]


def test_panel_listing_detail_and_related_card(app, lw, client):
    transport = httpx.MockTransport(json_api)
    app.add_url_rule(
        "/call", "call", lambda: httpx.Client(transport=transport).get("https://api.test/x").text
    )
    client.get("/call")
    entry = entries(lw, "http")[0]
    req = entries(lw, "request")[0]

    listing = client.get("/leeuwenhoek/http")
    assert listing.status_code == 200 and "https://api.test/x" in listing.text
    assert "HTTP client" in client.get("/leeuwenhoek/requests").text  # menu
    detail = client.get(f"/leeuwenhoek/http/{entry.uuid}")
    assert detail.status_code == 200 and "api.test" in detail.text
    related = client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    assert "HTTP client" in related and "https://api.test/x" in related
    assert client.get(f"/leeuwenhoek/batch/{req.batch_id}").status_code == 200


def test_panel_shows_errors(lw, client):
    def fail(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(httpx.ReadTimeout):
        httpx.Client(transport=httpx.MockTransport(fail)).get("https://slow.test/")
    entry = entries(lw, "http")[0]
    assert "ReadTimeout" in client.get("/leeuwenhoek/http").text
    assert "too slow" in client.get(f"/leeuwenhoek/http/{entry.uuid}").text


def test_redact_url():
    hidden = ["token", "password"]
    assert redact_url("https://h/p?Token=a&x=1&x=2", hidden) == f"https://h/p?Token={MASK}&x=1&x=2"
    assert redact_url("https://u:pw@h:8080/p", hidden) == f"https://u:{MASK}@h:8080/p"
    assert redact_url("https://h/p?q=a%20b#frag", hidden) == "https://h/p?q=a%20b#frag"
