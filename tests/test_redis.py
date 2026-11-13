import asyncio

import fakeredis
import pytest
import redis
import redis.asyncio
from conftest import entries
from flask import Flask
from flask_caching import Cache

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.context import internal
from leeuwenhoek.filters import MASK
from leeuwenhoek.watchers.redis_client import (
    MAX_ARG_CHARS,
    MAX_ARGS,
    MAX_PIPELINE_COMMANDS,
    RedisWatcher,
)


def originals():
    return (
        redis.client.Redis.execute_command,
        redis.client.Pipeline.execute,
        redis.client.Pipeline.immediate_execute_command,
        redis.asyncio.client.Redis.execute_command,
        redis.asyncio.client.Pipeline.execute,
        redis.asyncio.client.Pipeline.immediate_execute_command,
    )


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
def r(server):
    return fakeredis.FakeRedis(server=server)


def commands(lw):
    return [(e.content["command"], e.content["args"]) for e in reversed(entries(lw, "redis"))]


def test_commands_join_the_request_batch(app, lw, client, r):
    @app.get("/r")
    def view():
        r.set("greeting", "hello", ex=60)
        r.get("greeting")
        r.incr("visits")
        return "ok"

    client.get("/r")
    req = entries(lw, "request")[0]
    recorded = list(reversed(entries(lw, "redis")))
    assert commands(lw) == [
        ("SET", ["greeting", "hello", "EX", "60"]),
        ("GET", ["greeting"]),
        ("INCRBY", ["visits", "1"]),
    ]
    assert all(e.batch_id == req.batch_id for e in recorded)
    first = recorded[0].content
    assert first["db"] == 0 and first["host"].endswith(":6379")
    assert first["async"] is False and first["error"] is None
    assert first["duration_ms"] >= 0


def test_pipeline_is_one_entry(lw, r):
    with r.pipeline() as pipe:
        pipe.set("a", 1).incr("a").get("a")
        assert pipe.execute() == [True, 2, b"2"]
    with r.pipeline(transaction=False) as pipe:
        pipe.get("a")
        pipe.execute()
    recorded = [e.content for e in reversed(entries(lw, "redis"))]
    assert len(recorded) == 2  # the queued commands are not recorded on their own
    tx, plain = recorded
    assert tx["command"] == "PIPELINE" and tx["count"] == 3 and tx["transaction"] is True
    assert [(c["command"], c["args"]) for c in tx["commands"]] == [
        ("SET", ["a", "1"]),
        ("INCRBY", ["a", "1"]),
        ("GET", ["a"]),
    ]
    assert plain["transaction"] is False and plain["count"] == 1


def test_empty_pipeline_and_watch(lw, r):
    r.pipeline().execute()  # nothing queued: nothing recorded
    assert entries(lw, "redis") == []
    with r.pipeline() as pipe:
        pipe.watch("k")  # sent right away
        pipe.get("k")
        pipe.multi()
        pipe.set("k", "v")
        pipe.execute()
    assert [c for c, _ in commands(lw)] == ["WATCH", "GET", "PIPELINE"]


def test_pipeline_commands_are_capped_and_errors_kept(lw, r):
    r.set("text", "x")
    with r.pipeline(transaction=False) as pipe:
        for i in range(MAX_PIPELINE_COMMANDS + 5):
            pipe.set(f"k{i}", i)
        pipe.incr("text")  # fails inside the pipeline
        pipe.execute(raise_on_error=False)
    content = entries(lw, "redis")[0].content
    assert content["count"] == MAX_PIPELINE_COMMANDS + 6
    assert len(content["commands"]) == MAX_PIPELINE_COMMANDS
    assert content["commands_omitted"] == 6

    with r.pipeline(transaction=False) as pipe:
        pipe.incr("text").get("text")
        pipe.execute(raise_on_error=False)
    content = entries(lw, "redis")[0].content
    assert content["commands"][0]["error"]["class"] == "redis.exceptions.ResponseError"
    assert "error" not in content["commands"][1]


def test_failed_pipeline_is_recorded(lw, r):
    r.set("text", "x")
    with r.pipeline() as pipe:
        pipe.incr("text")
        with pytest.raises(redis.ResponseError):
            pipe.execute()
    content = entries(lw, "redis")[0].content
    assert content["command"] == "PIPELINE" and content["error"] is not None
    assert "failed" in entries(lw, "redis")[0].tags


def test_auth_and_hello_passwords_are_never_stored(lw, server):
    client = fakeredis.FakeRedis(server=server)
    with pytest.raises(redis.AuthenticationError):
        client.execute_command("AUTH", "hunter2")
    with pytest.raises(redis.AuthenticationError):
        client.execute_command("AUTH", "admin", "hunter2")
    fakeredis.FakeRedis(server=server).execute_command("HELLO", "3", "AUTH", "default", "hunter2")
    client.execute_command("CONFIG SET", "requirepass", "hunter2")
    auth, auth2, hello, config = commands(lw)
    assert auth == ("AUTH", [MASK])
    assert auth2 == ("AUTH", [MASK, MASK])
    assert hello == ("HELLO", ["3", "AUTH", "default", MASK])
    assert config == ("CONFIG", ["SET", "requirepass", MASK])
    assert all("hunter2" not in str(e.content) for e in entries(lw, "redis"))


def test_hidden_keys_and_fields_are_redacted(lw, r):
    r.set("user:1:token", "abc123", ex=10)
    r.hset("user:1", mapping={"name": "Ada", "password": "hunter2"})
    r.rpush("secrets", "a", "b")
    r.get("user:1:token")
    token, hset, rpush, get = commands(lw)
    assert token == ("SET", ["user:1:token", MASK, MASK, MASK])
    assert hset == ("HSET", ["user:1", "name", "Ada", "password", MASK])
    assert rpush == ("RPUSH", ["secrets", MASK, MASK])
    assert get == ("GET", ["user:1:token"])  # the key itself stays visible


def test_arguments_are_truncated_and_decoded(lw, r):
    r.set("big", "x" * 5000)
    r.set("bin", b"\xff\xfe\x00")
    r.rpush("list", *range(MAX_ARGS + 10))
    big, binary, many = (e.content for e in reversed(entries(lw, "redis")))
    assert big["args"][1].startswith("x" * MAX_ARG_CHARS)
    assert big["args"][1].endswith("[truncated: 5000 characters]")
    assert binary["args"] == ["bin", "[binary: 3 bytes]"]
    assert len(many["args"]) == MAX_ARGS and many["args_omitted"] == 11


def test_error_is_recorded_and_raised(lw, r):
    r.set("text", "x")
    with pytest.raises(redis.ResponseError):
        r.incr("text")
    entry = entries(lw, "redis")[0]
    assert entry.content["command"] == "INCRBY"
    assert entry.content["error"]["class"] == "redis.exceptions.ResponseError"
    assert "failed" in entry.tags


def test_standalone_outside_a_request(lw, r):
    r.ping()
    entry = entries(lw, "redis")[0]
    assert entry.content["command"] == "PING"
    assert entry.is_root


def test_asyncio_client_and_pipeline(lw, server):
    async def main():
        client = fakeredis.FakeAsyncRedis(server=server)
        await client.set("a", "1")
        async with client.pipeline() as pipe:
            pipe.incr("a").get("a")
            await pipe.execute()
        with pytest.raises(redis.ResponseError):
            await client.lpush("a", "x")

    asyncio.run(main())
    set_, pipe, failed = (e.content for e in reversed(entries(lw, "redis")))
    assert set_["command"] == "SET" and set_["async"] is True
    assert pipe["command"] == "PIPELINE" and pipe["count"] == 2 and pipe["async"] is True
    assert failed["error"]["class"] == "redis.exceptions.ResponseError"


def test_paused_internal_and_ignored_record_nothing(app, lw, client, r):
    app.add_url_rule("/health", "health", lambda: str(r.get("x")))
    app.add_url_rule("/r", "r", lambda: str(r.get("x")))
    client.get("/health")
    with internal():
        r.get("x")
    lw.set_recording(False)
    client.get("/r")
    r.get("x")
    assert entries(lw, "redis") == []


def test_redis_cache_backend_records_both_levels(app, lw, server, monkeypatch):
    monkeypatch.setattr(redis, "Redis", fakeredis.FakeRedis)
    monkeypatch.setattr(redis, "StrictRedis", fakeredis.FakeRedis)
    cache = Cache(app, config={"CACHE_TYPE": "RedisCache"})
    with app.app_context():
        cache.set("greeting", "hello")
        cache.get("greeting")
    assert [e.content["operation"] for e in reversed(entries(lw, "cache"))] == ["set", "get"]
    redis_commands = [c for c, _ in commands(lw)]
    assert "GET" in redis_commands and any(c in ("SET", "SETEX") for c in redis_commands)


def test_panel_listing_detail_and_related_card(app, lw, client, r):
    @app.get("/r")
    def view():
        r.set("greeting", "hello")
        with r.pipeline() as pipe:
            pipe.get("greeting").delete("greeting")
            pipe.execute()
        return "ok"

    client.get("/r")
    req = entries(lw, "request")[0]
    listing = client.get("/leeuwenhoek/redis")
    assert listing.status_code == 200 and "greeting" in listing.text
    assert 'href="/leeuwenhoek/redis"' in client.get("/leeuwenhoek/requests").text  # menu
    for entry in entries(lw, "redis"):
        assert client.get(f"/leeuwenhoek/redis/{entry.uuid}").status_code == 200
    pipe = entries(lw, "redis")[0]
    assert "DEL" in client.get(f"/leeuwenhoek/redis/{pipe.uuid}").text
    related = client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    assert "Redis" in related and "in total" in related
    assert client.get(f"/leeuwenhoek/batch/{req.batch_id}").status_code == 200
    assert client.get("/leeuwenhoek/redis/rows?after=0").status_code == 200


def test_unregister_in_order_leaves_no_patch(app, engine):
    before = originals()
    ext = Leeuwenhoek(app, engine=engine)
    assert redis.client.Redis.execute_command is not before[0]
    ext.close()
    assert originals() == before


def test_unregister_in_any_order_restores_the_clients(app, engine, r):
    other = Flask("other")
    other.config.update(app.config)
    other.debug = True
    before = originals()
    first = Leeuwenhoek(app, engine=engine)
    second = Leeuwenhoek(other, engine=engine)
    first.close()  # not on top: stays in the chain, only delegates
    r.ping()
    assert entries(first, "redis") == [] and len(entries(second, "redis")) == 1
    second.close()
    assert originals() == before
    r.ping()
    assert len(entries(second, "redis")) == 1


def test_watcher_metadata():
    assert RedisWatcher.requires == "redis"
    assert RedisWatcher.available()
