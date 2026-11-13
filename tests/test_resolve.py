"""Resolved exception families (#22), on both storage drivers."""

import os
from datetime import timedelta

import pytest
from conftest import entries

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.models import Entry, utcnow
from leeuwenhoek.storage import MemoryStorage, SQLAlchemyStorage

PG = os.environ.get("LEEUWENHOEK_TEST_PG_URI")


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no Postgres")),
    ]
)
def storage(request, tmp_path):
    if request.param == "memory":
        driver = MemoryStorage()
    else:
        driver = SQLAlchemyStorage(
            PG if request.param == "postgres" else f"sqlite:///{tmp_path}/lw.db"
        )
    driver.install()
    if request.param == "postgres":  # shared database: start from scratch
        driver.clear()
        for key in driver.settings():
            driver.delete_setting(key)
    yield driver
    driver.close()


def exc(family, when, **kw):
    return Entry(
        type="exception",
        content={"class": "builtins.RuntimeError", "message": family},
        batch_id="b",
        family_hash=family,
        created_at=when,
        **kw,
    )


def test_settings_api(storage):
    storage.set_setting("resolved:a", "1")
    storage.set_setting("resolved:b", "2")
    storage.set_setting("resolved_", "x")  # "_" is not a LIKE wildcard here
    storage.set_setting("recording", "0")
    assert storage.settings("resolved:") == {"resolved:a": "1", "resolved:b": "2"}
    assert len(storage.settings()) == 4
    storage.delete_setting("resolved:a")
    storage.delete_setting("missing")  # no error
    assert storage.settings("resolved:") == {"resolved:b": "2"}


def test_resolve_hides_and_newer_occurrence_reopens(storage):
    now = utcnow()
    storage.save(
        [
            exc("aaa", now - timedelta(minutes=5)),
            exc("bbb", now - timedelta(minutes=4)),
            exc("aaa", now - timedelta(minutes=3)),
            Entry(type="exception", content={}, batch_id="b"),  # no family: never hidden
        ]
    )
    assert storage.last_seen(["aaa", "bbb", "zzz"]) == {
        "aaa": now - timedelta(minutes=3),
        "bbb": now - timedelta(minutes=4),
    }
    assert storage.resolved_families() == {}

    storage.resolve_family("aaa", at=now - timedelta(minutes=1))
    resolved = storage.resolved_families()
    assert resolved == {"aaa": now - timedelta(minutes=1)}
    assert storage.family_resolved_at("aaa") == now - timedelta(minutes=1)
    assert storage.family_resolved_at("bbb") is None

    # Hidden from the listing and its pages, shown again without the exclusion.
    kept = storage.list("exception", exclude_families=resolved)
    assert [e.family_hash for e in kept] == [None, "bbb"]
    assert len(storage.list("exception")) == 4
    page = storage.page("exception", exclude_families=resolved, limit=1)
    assert page.older is not None
    older = storage.page("exception", exclude_families=resolved, before=page.older, limit=1)
    assert [e.family_hash for e in older.entries] == ["bbb"] and older.older is None

    # An occurrence from before the resolve time (e.g. flushed late) keeps it resolved...
    storage.save([exc("aaa", now - timedelta(minutes=2))])
    assert "aaa" in storage.resolved_families()

    # ...a newer one reopens the family, with every occurrence back in the listing.
    storage.save([exc("aaa", now)])
    assert storage.resolved_families() == {}
    assert storage.family_resolved_at("aaa") is None
    assert len(storage.list("exception", exclude_families=storage.resolved_families())) == 6

    # Resolving again covers the new occurrence; reopening removes the setting.
    storage.resolve_family("aaa", at=now + timedelta(seconds=1))
    assert set(storage.resolved_families()) == {"aaa"}
    storage.reopen_family("aaa")
    assert storage.resolved_families() == {}
    assert storage.settings("resolved:") == {}


def test_family_without_entries_is_not_resolved(storage):
    storage.resolve_family("gone")
    assert storage.resolved_families() == {}
    assert storage.family_resolved_at("gone") is None


# ------------------------------------------------------------------------- panel


@pytest.fixture(params=["memory", "sqlalchemy"])
def panel(request, app, engine, tmp_path):
    if request.param == "sqlalchemy":
        app.config.update(
            LEEUWENHOEK_STORAGE="sqlalchemy",
            LEEUWENHOEK_DATABASE_URI=f"sqlite:///{tmp_path}/lw.db",
        )
    ext = Leeuwenhoek(app, engine=engine)

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.get("/other")
    def other():
        raise ValueError("another family")

    client = app.test_client()
    client.get("/boom")
    client.get("/other")
    client.get("/leeuwenhoek/exceptions")  # issues the CSRF cookie
    yield ext, client
    ext.close()


def families(lw):
    by_class = {e.content["class"]: e.family_hash for e in entries(lw, "exception")}
    return by_class["builtins.RuntimeError"], by_class["builtins.ValueError"]


def token(client):
    return client.get_cookie("lw_csrf").value


def test_resolve_requires_csrf(panel):
    lw, client = panel
    boom, _ = families(lw)
    url = f"/leeuwenhoek/exceptions/family/{boom}/resolve"
    assert client.post(url).status_code == 400
    assert client.post(url, data={"csrf_token": "forged"}).status_code == 400
    assert client.post(f"/leeuwenhoek/exceptions/family/{boom}/reopen").status_code == 400
    assert lw.storage.resolved_families() == {}
    assert client.post(url, data={"csrf_token": token(client)}).status_code == 302
    assert set(lw.storage.resolved_families()) == {boom}
    unknown = "/leeuwenhoek/exceptions/family/nope/resolve"
    assert client.post(unknown, data={"csrf_token": token(client)}).status_code == 404


def test_listing_hides_resolved_shows_with_filter_and_reopens(panel):
    lw, client = panel
    boom, other = families(lw)
    html = client.get("/leeuwenhoek/exceptions").text
    assert "kaboom" in html and "another family" in html
    assert f"/exceptions/family/{boom}/resolve" in html  # Resolve button on each row

    resp = client.post(
        f"/leeuwenhoek/exceptions/family/{boom}/resolve",
        data={"csrf_token": token(client)},
        headers={"Referer": "/leeuwenhoek/exceptions"},
    )
    assert resp.status_code == 302 and resp.location.endswith("/leeuwenhoek/exceptions")

    # Hidden by default, in the listing and in live polling.
    html = client.get("/leeuwenhoek/exceptions").text
    assert "kaboom" not in html and "another family" in html
    assert "Show resolved" in html and "resolved=1" in html
    rows = client.get("/leeuwenhoek/exceptions/rows").text
    assert "kaboom" not in rows and "another family" in rows

    # Shown with ?resolved=1, marked as resolved, with a Reopen button; polling keeps the filter.
    html = client.get("/leeuwenhoek/exceptions?resolved=1").text
    assert "kaboom" in html and "Hide resolved" in html
    assert f"/exceptions/family/{boom}/reopen" in html
    assert 'data-rows-url="/leeuwenhoek/exceptions/rows?resolved=1"' in html
    assert "kaboom" in client.get("/leeuwenhoek/exceptions/rows?resolved=1").text

    # Detail and family pages show the state and a Reopen button.
    exc = next(e for e in entries(lw, "exception") if e.family_hash == boom)
    for page in (f"/leeuwenhoek/exceptions/{exc.uuid}", f"/leeuwenhoek/exceptions/family/{boom}"):
        html = client.get(page).text
        assert "Resolved" in html and f"/exceptions/family/{boom}/reopen" in html
    html = client.get(f"/leeuwenhoek/exceptions/family/{other}").text
    assert f"/exceptions/family/{other}/resolve" in html

    # A new occurrence reopens the family; polling picks it up as a new row.
    latest = max(e.id for e in entries(lw, "exception"))
    client.get("/boom")
    assert lw.storage.resolved_families() == {}
    assert "kaboom" in client.get(f"/leeuwenhoek/exceptions/rows?after={latest}").text
    assert client.get("/leeuwenhoek/exceptions").text.count("kaboom") == 2


def test_reopen_button(panel):
    lw, client = panel
    boom, _ = families(lw)
    lw.storage.resolve_family(boom)
    resp = client.post(
        f"/leeuwenhoek/exceptions/family/{boom}/reopen", data={"csrf_token": token(client)}
    )
    assert resp.status_code == 302 and resp.location.endswith(f"/exceptions/family/{boom}")
    assert lw.storage.resolved_families() == {}
    assert "kaboom" in client.get("/leeuwenhoek/exceptions").text


def test_resolved_param_only_applies_to_exceptions(panel):
    _, client = panel
    html = client.get("/leeuwenhoek/requests?resolved=1").text
    assert "resolved=1" not in html and "Show resolved" not in html
