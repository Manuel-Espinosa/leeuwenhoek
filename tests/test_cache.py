import pytest
from conftest import entries
from flask import Flask
from flask_caching import Cache

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.filters import MASK
from leeuwenhoek.watchers.cache import METHODS, CacheWatcher, hidden_key


@pytest.fixture
def cache(app):
    return Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})


@pytest.fixture
def cached_app(app, cache, lw):
    @app.get("/cached")
    def cached():
        cache.set("greeting", "hello")
        cache.set("short", 1, timeout=5)
        cache.get("greeting")  # hit
        cache.get("missing")  # miss
        cache.get_many("greeting", "short", "nope")  # 2 hits, 1 miss
        cache.delete("short")
        return "ok"

    return app


def ops(lw):
    return [e.content["operation"] for e in reversed(entries(lw, "cache"))]


def test_operations_join_the_request_batch(cached_app, client, lw):
    client.get("/cached")
    req = entries(lw, "request")[0]
    cache_entries = list(reversed(entries(lw, "cache")))
    assert [e.content["operation"] for e in cache_entries] == [
        "set",
        "set",
        "get",
        "get",
        "get_many",
        "delete",
    ]
    assert all(e.batch_id == req.batch_id for e in cache_entries)
    set_default, set_short, hit, miss, many, delete = (e.content for e in cache_entries)
    assert set_default["keys"] == ["greeting"] and set_default["timeout"] == 300
    assert set_short["timeout"] == 5 and set_short["result"] is True
    assert hit["hit"] is True and (hit["hits"], hit["misses"]) == (1, 0)
    assert miss["hit"] is False and (miss["hits"], miss["misses"]) == (0, 1)
    assert many["keys"] == ["greeting", "short", "nope"]
    assert (many["hits"], many["misses"]) == (2, 1)
    assert delete["keys"] == ["short"]
    assert hit["backend"] == "SimpleCache"
    assert "values" not in hit  # values are opt-in


def test_request_detail_shows_hit_and_miss_counts(cached_app, client, lw):
    client.get("/cached")
    req = entries(lw, "request")[0]
    page = client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    assert "Cache" in page
    assert "3 hits" in page and "2 misses" in page
    assert client.get(f"/leeuwenhoek/batch/{req.batch_id}").status_code == 200


def test_panel_listing_and_detail(cached_app, client, lw):
    client.get("/cached")
    listing = client.get("/leeuwenhoek/cache")
    assert listing.status_code == 200
    assert "greeting" in listing.text
    assert 'href="/leeuwenhoek/cache"' in client.get("/leeuwenhoek/requests").text
    for entry in entries(lw, "cache"):
        assert client.get(f"/leeuwenhoek/cache/{entry.uuid}").status_code == 200
    assert client.get("/leeuwenhoek/cache/rows?after=0").status_code == 200


def test_standalone_outside_a_request(app, cache, lw):
    with app.app_context():
        cache.set("k", "v")
    # Cache(app) also works without an app context.
    assert cache.get("k") == "v"
    recorded = list(reversed(entries(lw, "cache")))
    assert [e.content["operation"] for e in recorded] == ["set", "get"]
    assert all(e.is_root for e in recorded)
    assert lw.storage.get(recorded[0].uuid) is not None


def test_nested_calls_are_recorded_once(app, cache, lw):
    with app.app_context():
        cache.set_many({"a": 1, "b": 2})
        cache.unlink("a", "b")  # calls delete_many internally
        cache.clear()
        cache.add("c", 3)
        cache.has("c")
        cache.get_dict("c", "d")
    assert ops(lw) == ["set_many", "unlink", "clear", "add", "has", "get_dict"]
    set_many, unlink, clear, add, has, get_dict = (
        e.content for e in reversed(entries(lw, "cache"))
    )
    assert set_many["keys"] == ["a", "b"] and set_many["timeout"] == 300
    assert unlink["keys"] == ["a", "b"]
    assert clear["keys"] == []
    assert add["result"] is True
    assert has["hit"] is True
    assert (get_dict["hits"], get_dict["misses"]) == (1, 1)


def test_cached_decorator(app, cache, lw, client):
    @app.get("/view")
    @cache.cached(timeout=60)
    def view():
        return "body"

    client.get("/view")
    client.get("/view")
    first, second = (
        [e.content for e in reversed(entries(lw, "cache")) if e.batch_id == r.batch_id]
        for r in reversed(entries(lw, "request"))
    )
    assert [c["operation"] for c in first] == ["get", "set"]
    assert first[0]["hit"] is False and first[1]["timeout"] == 60
    assert [c["operation"] for c in second] == ["get"] and second[0]["hit"] is True


def test_values_are_optional_redacted_and_truncated(app, engine):
    app.config.update(LEEUWENHOEK_CACHE_VALUES=True, LEEUWENHOEK_MAX_BODY_KB=1)
    ext = Leeuwenhoek(app, engine=engine)
    cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})
    try:
        with app.app_context():
            cache.set("profile", {"name": "Ada", "password": "hunter2"})
            cache.set("user:1:token", "abc123")
            cache.set("big", "x" * 5000)
            cache.get("profile")
        profile, token, big, read = (e.content for e in reversed(entries(ext, "cache")))
        assert profile["values"] == {"profile": {"name": "Ada", "password": MASK}}
        assert token["values"] == {"user:1:token": MASK}
        assert token["keys"] == ["user:1:token"]  # keys stay visible
        assert big["values"]["big"].endswith("[truncated: 5000 characters]")
        assert read["values"] == {"profile": {"name": "Ada", "password": MASK}}
    finally:
        ext.close()


def test_hidden_key():
    hidden = ["password", "Token"]
    assert hidden_key("user:1:token", hidden)
    assert hidden_key("RESET_PASSWORD", hidden)
    assert not hidden_key("profile", hidden)


def test_paused_or_ignored_requests_record_nothing(app, cache, lw, client):
    app.add_url_rule("/health", "health", lambda: cache.get("x") or "ok")
    client.get("/health")
    assert entries(lw, "cache") == []


def test_unregister_restores_cache_methods(app, engine):
    other = Flask("other")
    other.config.update(app.config)
    originals = {name: Cache.__dict__[name] for name in METHODS}
    first = Leeuwenhoek(app, engine=engine)
    patched = Cache.__dict__["get"]
    assert patched is not originals["get"]
    second = Leeuwenhoek(other, engine=engine)
    assert Cache.__dict__["get"] is patched  # patched once, shared
    first.close()
    assert Cache.__dict__["get"] is patched  # still used by the second instance
    second.close()
    assert {name: Cache.__dict__[name] for name in METHODS} == originals


def test_not_recorded_after_close(app, engine):
    ext = Leeuwenhoek(app, engine=engine)
    cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})
    ext.close()
    with app.app_context():
        cache.set("k", "v")
    assert entries(ext, "cache") == []


def test_watcher_metadata():
    assert CacheWatcher.requires == "flask_caching"
    assert CacheWatcher.available()


def test_memoize(app, cache, lw):
    calls = []

    @cache.memoize(timeout=30)
    def square(n):
        calls.append(n)
        return n * n

    with app.app_context():
        assert square(3) == 9
        assert square(3) == 9
    assert calls == [3]
    reads = [e.content for e in entries(lw, "cache") if e.content["operation"] == "get"]
    assert sorted(r["hit"] for r in reads) == [False, True]
