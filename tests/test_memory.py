import tracemalloc

import pytest
from conftest import entries

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers.request import memory_tracker


@pytest.fixture
def tracked(app, engine):
    app.config["LEEUWENHOEK_TRACK_MEMORY"] = True
    ext = Leeuwenhoek(app, engine=engine)

    @app.get("/alloc")
    def alloc():
        data = bytearray(2 * 1024 * 1024)  # 2 MB alive while the view runs
        return {"size": len(data)}

    yield ext
    ext.close()


def test_off_by_default(client, lw):
    client.get("/hello")
    assert "memory_peak_kb" not in entries(lw, "request")[0].content
    assert not tracemalloc.is_tracing()


def test_peak_recorded_and_tracing_stopped(app, tracked):
    assert not tracemalloc.is_tracing()
    app.test_client().get("/alloc")
    peak = entries(tracked, "request")[0].content["memory_peak_kb"]
    assert peak >= 2000  # the 2 MB buffer, minus a little transient noise
    assert not tracemalloc.is_tracing()  # started by us, so stopped by us


def test_pre_started_tracing_is_left_running(app, tracked):
    tracemalloc.start()
    try:
        keep = bytearray(4 * 1024 * 1024)  # traced before the request: not part of its peak
        app.test_client().get("/alloc")
        assert tracemalloc.is_tracing()
        peak = entries(tracked, "request")[0].content["memory_peak_kb"]
        assert 2000 <= peak < 4096
        del keep
    finally:
        tracemalloc.stop()


def test_not_tracked_when_not_recording(app, tracked):
    tracked.set_recording(False)
    app.test_client().get("/alloc")
    assert entries(tracked) == []
    assert not tracemalloc.is_tracing()


def test_detail_shows_peak(app, tracked):
    client = app.test_client()
    client.get("/alloc")
    entry = entries(tracked, "request")[0]
    resp = client.get(f"/leeuwenhoek/requests/{entry.uuid}")
    assert resp.status_code == 200
    assert "Peak memory" in resp.text and " MB" in resp.text


def test_overlapping_requests_share_tracing():
    first = memory_tracker.start()
    second = memory_tracker.start()
    memory_tracker.stop(first)
    assert tracemalloc.is_tracing()  # the second one is still running
    memory_tracker.stop(second)
    assert not tracemalloc.is_tracing()
