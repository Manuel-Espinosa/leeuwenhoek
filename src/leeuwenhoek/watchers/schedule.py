"""APScheduler 3.x jobs: each run gets its own batch, with the job, its trigger, result and time."""

from __future__ import annotations

import functools
import inspect
import operator
import time
import weakref
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask

from ..context import current_batch, is_internal
from ..filters import limit_payload, redact
from .base import Watcher
from .exception import ExceptionWatcher
from .http_client import _Patch

#: characters of ``repr(result)`` that are kept
MAX_RESULT_CHARS = 1000


class ScheduleWatcher(Watcher):
    name = "schedule"
    entry_type = "schedule"
    label = "Schedule"
    requires = "apscheduler"

    def register(self, app: Flask) -> None:
        from apscheduler.events import EVENT_JOB_MISSED
        from apscheduler.executors.base import BaseExecutor, MaxInstancesReachedError

        self._missed_mask = EVENT_JOB_MISSED
        self._max_instances_error = MaxInstancesReachedError
        # Schedulers we listen to for missed runs, and what we last saw of each job, so a missed
        # run (an event that only carries the job id) still gets its name and trigger.
        self._schedulers: weakref.WeakSet[Any] = weakref.WeakSet()
        self._jobs: dict[str, dict[str, Any]] = {}
        # Every executor (thread and process pools, asyncio, gevent, debug...) is handed each
        # due run through ``BaseExecutor.submit_job``. The job is wrapped there rather than when
        # it is added, because persistent job stores rebuild the job, and its original function,
        # every time it is due.
        self._patch = _Patch(BaseExecutor, "submit_job", self._wrap_submit)

    def unregister(self) -> None:
        self._patch.undo()
        for scheduler in list(self._schedulers):
            scheduler.remove_listener(self._on_missed)
        self._schedulers = weakref.WeakSet()

    # ------------------------------------------------------------------ hooks

    def _wrap_submit(self, patch: _Patch) -> Callable[..., Any]:
        watcher = self

        def submit_job(executor: Any, job: Any, run_times: list[datetime]) -> Any:
            if not patch.active or is_internal():
                return patch.original(executor, job, run_times)
            try:
                wrapped = watcher._prepare(executor, job, run_times)
            except Exception:  # noqa: BLE001 - recording must never keep a job from running
                wrapped = job
            try:
                return patch.original(executor, wrapped, run_times)
            except watcher._max_instances_error:
                watcher._record_skipped(job, run_times)
                raise

        return submit_job

    def _prepare(self, executor: Any, job: Any, run_times: list[datetime]) -> Any:
        info = describe(job)
        self._jobs[job.id] = info
        scheduler = getattr(executor, "_scheduler", None)
        if scheduler is not None and scheduler not in self._schedulers:
            scheduler.add_listener(self._on_missed, self._missed_mask)
            self._schedulers.add(scheduler)
        info = {**info, "next_run_time": iso(next_fire_time(job, run_times))}
        batch = current_batch()  # e.g. a command that runs a BlockingScheduler
        parent_id = batch.id if batch is not None and batch.recording else None
        slots = RunTimes(run_times, job.misfire_grace_time)
        return RecordedJob(job, self._wrap_func(job.func, info, slots, parent_id))

    def _wrap_func(
        self, func: Callable[..., Any], info: dict[str, Any], slots: RunTimes, parent_id: str | None
    ) -> Callable[..., Any]:
        watcher = self

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                run_time = slots.take()
                with watcher.lw.batch("schedule", parent_id=parent_id) as batch:
                    if not batch.recording:
                        return await func(*args, **kwargs)
                    try:
                        result = await func(*args, **kwargs)
                    except BaseException as e:
                        watcher._failed(info, run_time, args, kwargs, e, started)
                        raise
                    watcher._succeeded(info, run_time, args, kwargs, result, started)
                    return result

            return run_async

        @functools.wraps(func)
        def run(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            run_time = slots.take()
            with watcher.lw.batch("schedule", parent_id=parent_id) as batch:
                if not batch.recording:
                    return func(*args, **kwargs)
                try:
                    result = func(*args, **kwargs)
                except BaseException as e:
                    watcher._failed(info, run_time, args, kwargs, e, started)
                    raise
                watcher._succeeded(info, run_time, args, kwargs, result, started)
                return result

        return run

    # ------------------------------------------------------------------ recording

    def _succeeded(
        self,
        info: dict[str, Any],
        run_time: datetime | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result: Any,
        started: float,
    ) -> None:
        content = self._content(info, "success", run_time, args, kwargs, started)
        content["result"] = short_repr(result)
        self._record_run(info, content, is_root=True)

    def _failed(
        self,
        info: dict[str, Any],
        run_time: datetime | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        exc: BaseException,
        started: float,
    ) -> None:
        if isinstance(exc, Exception):
            # Recorded here so it joins this batch; APScheduler catches it and only logs it.
            exceptions = self.lw.watchers.get("exception")
            if isinstance(exceptions, ExceptionWatcher):
                exceptions.capture(exc, handled=True)
        content = self._content(info, "error", run_time, args, kwargs, started)
        content["exception"] = {"class": type(exc).__qualname__, "message": str(exc)}
        self._record_run(info, content, is_root=True)

    def _record_skipped(self, job: Any, run_times: list[datetime]) -> None:
        info = self._jobs.get(job.id) or describe(job)
        content = self._content(info, "skipped", run_times[-1] if run_times else None)
        content["reason"] = f"maximum number of running instances reached ({job.max_instances})"
        self._record_run(info, content)

    def _on_missed(self, event: Any) -> None:
        info = self._jobs.get(event.job_id) or {"id": event.job_id, "name": event.job_id}
        content = self._content(info, "missed", event.scheduled_run_time)
        content["reason"] = "run time missed by more than the misfire grace time"
        self._record_run(info, content)

    def _content(
        self,
        info: dict[str, Any],
        status: str,
        run_time: datetime | None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        started: float | None = None,
    ) -> dict[str, Any]:
        cfg = self.lw.config
        payload = {"args": list(args), "kwargs": dict(kwargs or {})}
        return {
            "id": info.get("id"),
            "name": info.get("name"),
            "func": info.get("func"),
            "trigger": info.get("trigger"),
            "executor": info.get("executor"),
            "status": status,
            "scheduled_run_time": iso(run_time),
            "next_run_time": info.get("next_run_time"),
            "arguments": limit_payload(redact(payload, cfg.hidden_params), cfg.max_body_kb),
            "duration_ms": (
                None if started is None else round((time.perf_counter() - started) * 1000, 2)
            ),
        }

    def _record_run(self, info: dict[str, Any], content: dict[str, Any], **kwargs: Any) -> None:
        tags = [f"job:{info.get('id')}", f"job_status:{content['status']}"]
        self.record(content, tags=tags, force=True, **kwargs)


class RecordedJob:
    """Stands for an APScheduler ``Job`` in one submission, with ``func`` wrapped.

    Everything but ``func`` is read from the job. Pickled (by the process pool executor), it
    turns back into the original job, so the run happens unrecorded in the child process.
    """

    __slots__ = ("_job", "func")

    def __init__(self, job: Any, func: Callable[..., Any]) -> None:
        self._job = job
        self.func = func

    def __getattr__(self, name: str) -> Any:
        return getattr(self._job, name)

    def __str__(self) -> str:
        return str(self._job)

    def __repr__(self) -> str:
        return repr(self._job)

    def __reduce__(self) -> Any:
        # ``itemgetter(0)((job,))`` is the job itself: rebuilding it needs nothing but the
        # standard library, so the child process doesn't have to import Leeuwenhoek.
        return (operator.itemgetter(0), ((self._job,),))


class RunTimes:
    """Hands each call of a submitted job the run time it is for.

    ``run_job`` loops over the run times (several only with ``coalesce=False``) and skips the
    ones past the misfire grace time; the same check is repeated here, so the attribution is
    exact unless a run time crosses the grace limit between both checks.
    """

    def __init__(self, run_times: list[datetime], grace: float | None) -> None:
        self._pending = list(run_times)
        self._grace = None if grace is None else timedelta(seconds=grace)

    def take(self) -> datetime | None:
        now = datetime.now(timezone.utc)
        while len(self._pending) > 1 and self._grace is not None:
            if now - self._pending[0] <= self._grace:
                break
            self._pending.pop(0)
        return self._pending.pop(0) if self._pending else None


def describe(job: Any) -> dict[str, Any]:
    func = getattr(job, "func_ref", None) or getattr(job, "func", None)
    if not isinstance(func, str):
        func = getattr(func, "__qualname__", None) or repr(func)
    return {
        "id": job.id,
        "name": job.name,
        "func": func,
        "trigger": str(job.trigger),
        "executor": getattr(job, "executor", None),
    }


def next_fire_time(job: Any, run_times: list[datetime]) -> datetime | None:
    """What the scheduler will set as ``next_run_time`` once this submission is done."""
    if not run_times:
        return None
    try:
        return job.trigger.get_next_fire_time(run_times[-1], datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - a custom trigger failing here is not our concern
        return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def short_repr(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - any object can have a broken __repr__
        text = f"<{type(value).__qualname__} (repr failed)>"
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"… [truncated: {len(text)} characters]"
