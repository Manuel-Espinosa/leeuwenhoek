"""APScheduler jobs: one batch per run."""

import asyncio
import logging
import pickle
import threading
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.executors.base import BaseExecutor, MaxInstancesReachedError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from conftest import entries
from sqlalchemy import text

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers.schedule import RecordedJob, RunTimes


def tick():
    return "tick"


def runs(lw):
    return entries(lw, "schedule")


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def scheduler():
    scheduler = BackgroundScheduler(timezone=timezone.utc)
    yield scheduler
    if scheduler.running:
        scheduler.shutdown(wait=True)


def run_once(scheduler, func, mask=EVENT_JOB_EXECUTED | EVENT_JOB_ERROR, **kwargs):
    """Run ``func`` once now and wait until the run, and its callbacks, are done."""
    done = threading.Event()
    scheduler.add_listener(lambda event: done.set(), mask)
    kwargs.setdefault("next_run_time", now())
    scheduler.add_job(func, **kwargs)
    scheduler.start()
    assert done.wait(5)
    scheduler.shutdown(wait=True)  # joins the pool threads, where the events are dispatched


def test_job_batch_groups_query_log_and_run(app, lw, engine, scheduler):
    def sync_users(limit, token=None):
        with app.app_context():
            logging.getLogger("app").warning("syncing %s users", limit)
            with engine.connect() as conn:
                conn.execute(text("select 1"))  # in-memory SQLite: no table in other threads
        return {"synced": limit}

    run_once(
        scheduler,
        sync_users,
        trigger="interval",
        minutes=5,
        id="sync",
        name="Sync users",
        args=[10],
        kwargs={"token": "abc"},
    )
    [run] = runs(lw)
    assert run.is_root
    assert sorted(e.type for e in lw.storage.batch(run.batch_id)) == ["log", "query", "schedule"]
    c = run.content
    assert c["id"] == "sync" and c["name"] == "Sync users"
    assert c["func"].endswith("sync_users")
    assert c["trigger"].startswith("interval[0:05:00]")
    assert c["executor"] == "default"
    assert c["status"] == "success"
    assert c["result"] == "{'synced': 10}"
    assert c["arguments"] == {"args": [10], "kwargs": {"token": "********"}}
    assert c["duration_ms"] >= 0
    scheduled = datetime.fromisoformat(c["scheduled_run_time"])
    assert datetime.fromisoformat(c["next_run_time"]) - scheduled == timedelta(minutes=5)
    assert {"job:sync", "job_status:success"} <= set(run.tags)


def test_failing_job_is_persisted_with_its_exception(lw, engine, scheduler):
    def explode():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        raise RuntimeError("job boom")

    run_once(scheduler, explode, id="explode")
    [run] = runs(lw)
    [exc] = entries(lw, "exception")
    assert run.content["status"] == "error"
    assert run.content["exception"] == {"class": "RuntimeError", "message": "job boom"}
    assert "result" not in run.content
    assert exc.batch_id == run.batch_id and exc.content["handled"] is True
    assert {"schedule", "exception", "query"} <= {e.type for e in lw.storage.batch(run.batch_id)}
    assert "job_status:error" in run.tags


def test_each_run_gets_its_own_batch(lw, scheduler):
    done = threading.Event()
    count = []

    def counted():
        count.append(1)
        if len(count) == 2:
            done.set()

    scheduler.add_job(counted, "interval", seconds=3600, id="a", next_run_time=now())
    scheduler.add_job(counted, "interval", seconds=3600, id="b", next_run_time=now())
    scheduler.start()
    assert done.wait(5)
    scheduler.shutdown(wait=True)
    batches = {r.batch_id for r in runs(lw)}
    assert len(batches) == 2


def test_missed_run(lw, scheduler):
    run_once(
        scheduler,
        tick,
        mask=EVENT_JOB_MISSED,
        id="late",
        next_run_time=now() - timedelta(hours=1),
        misfire_grace_time=1,
    )
    [run] = runs(lw)
    assert run.content["status"] == "missed" and run.content["id"] == "late"
    assert run.content["func"].endswith("tick")
    assert run.content["duration_ms"] is None


def test_max_instances_reached(lw, scheduler):
    release = threading.Event()
    scheduler.start()
    job = scheduler.add_job(release.wait, id="slow", max_instances=1, next_run_time=None)
    executor = scheduler._lookup_executor("default")
    executor.submit_job(job, [now()])
    with pytest.raises(MaxInstancesReachedError):
        executor.submit_job(job, [now()])
    release.set()
    scheduler.shutdown(wait=True)
    statuses = sorted(r.content["status"] for r in runs(lw))
    assert statuses == ["skipped", "success"]
    [skipped] = [r for r in runs(lw) if r.content["status"] == "skipped"]
    assert "maximum number of running instances" in skipped.content["reason"]


def test_coroutine_job(lw):
    async def main():
        done = asyncio.Event()
        scheduler = AsyncIOScheduler(timezone=timezone.utc)

        async def ping():
            await asyncio.sleep(0)
            logging.getLogger("app").warning("pong")
            return "pong"

        scheduler.add_listener(lambda event: done.set(), EVENT_JOB_EXECUTED)
        scheduler.add_job(ping, id="ping", next_run_time=now())
        scheduler.start()
        await asyncio.wait_for(done.wait(), 5)
        scheduler.shutdown(wait=False)

    asyncio.run(main())
    [run] = runs(lw)
    assert run.content["status"] == "success" and run.content["result"] == "'pong'"
    assert sorted(e.type for e in lw.storage.batch(run.batch_id)) == ["log", "schedule"]


def test_process_pool_pickles_the_original_job(lw, scheduler):
    scheduler.start(paused=True)  # pending jobs are not complete enough to be pickled
    job = scheduler.add_job(tick, id="pickled")
    proxy = RecordedJob(job, lambda: None)
    assert str(proxy) == str(job) and proxy.id == "pickled"
    restored = pickle.loads(pickle.dumps(proxy))
    assert type(restored) is type(job) and restored.func is tick


def test_run_times_skip_missed_ones():
    t = now()
    slots = RunTimes([t - timedelta(hours=1), t], grace=1)
    assert slots.take() == t
    assert slots.take() is None
    assert RunTimes([t - timedelta(hours=1)], grace=1).take() == t - timedelta(hours=1)


def test_disabled(app, scheduler):
    app.config["LEEUWENHOEK_ENABLED"] = False
    ext = Leeuwenhoek(app)
    run_once(scheduler, tick)
    assert runs(ext) == []
    ext.close()


def test_only_errors_keeps_failing_runs(app, scheduler):
    app.config["LEEUWENHOEK_ONLY_ERRORS"] = True
    ext = Leeuwenhoek(app)
    done = threading.Event()
    seen = []

    def finished(event):
        seen.append(event)
        if len(seen) == 2:
            done.set()

    def fail():
        raise RuntimeError("boom")

    scheduler.add_listener(finished, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.add_job(tick, id="ok", next_run_time=now())
    scheduler.add_job(fail, id="fail", next_run_time=now())
    scheduler.start()
    assert done.wait(5)
    scheduler.shutdown(wait=True)
    assert [r.content["id"] for r in runs(ext)] == ["fail"]
    ext.close()


def test_submit_job_restored_after_close(app, scheduler):
    original = BaseExecutor.submit_job
    ext = Leeuwenhoek(app)
    assert BaseExecutor.submit_job is not original
    ext.close()
    assert BaseExecutor.submit_job is original
    run_once(scheduler, tick)
    assert runs(ext) == []


def test_listeners_removed_after_close(app, scheduler):
    ext = Leeuwenhoek(app)
    run_once(scheduler, tick)
    watcher = ext.watchers["schedule"]
    assert any(listener[0] == watcher._on_missed for listener in scheduler._listeners)
    ext.close()
    assert not any(listener[0] == watcher._on_missed for listener in scheduler._listeners)


def test_panel(app, lw, scheduler):
    run_once(scheduler, tick, id="tick", name="Tick")
    [run] = runs(lw)
    client = app.test_client()
    listing = client.get("/leeuwenhoek/schedule").get_data(as_text=True)
    assert "Tick" in listing and "success" in listing
    detail = client.get(f"/leeuwenhoek/schedule/{run.uuid}").get_data(as_text=True)
    assert "Next run time" in detail and "Scheduled run time" in detail and "tick" in detail
    batch = client.get(f"/leeuwenhoek/batch/{run.batch_id}").get_data(as_text=True)
    assert "Scheduled run time" in batch
    menu = client.get("/leeuwenhoek/requests").get_data(as_text=True)
    assert 'href="/leeuwenhoek/schedule"' in menu
