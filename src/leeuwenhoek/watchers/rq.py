"""RQ jobs: each execution gets its own batch, linked to the batch that enqueued the job."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from flask import Flask

from ..context import _internal, current_batch, internal, is_internal
from ..filters import limit_payload, redact
from ..models import jsonable
from .base import Watcher
from .exception import ExceptionWatcher

#: ``job.meta`` key holding the id of the batch that enqueued the job
PARENT_KEY = "leeuwenhoek_parent_batch_id"
#: ``batch.meta`` key marking a job batch with the id of the job it runs
JOB_KEY = "rq_job_id"
#: ``job.meta`` key set by the panel's Retry button: uuid of the failed execution it retries
RETRY_KEY = "leeuwenhoek_retry_of"


class RQWatcher(Watcher):
    name = "rq"
    entry_type = "rq"
    label = "RQ jobs"
    requires = "rq"

    def register(self, app: Flask) -> None:
        from rq import Queue, Worker
        from rq.job import Job

        self._active = True
        # ``create_job`` is where every job is built (``enqueue``, ``enqueue_at``,
        # ``enqueue_many``...), so the dispatching batch goes into ``job.meta`` there, before
        # the job is saved. ``enqueue_job`` records the dispatch. ``Job.perform`` runs the job
        # function in every execution path: the work horse of a forking ``Worker``, a
        # ``SimpleWorker`` and ``Queue(is_async=False)``. Chained, never replaced, like the
        # command watcher's ``invoke``. ``Worker.work`` (the polling loop, heartbeats, registry
        # maintenance and its own logs) runs as internal, so it records nothing: only what
        # happens inside ``Job.perform`` does.
        self._patches: list[tuple[type, str, Callable[..., Any], Callable[..., Any]]] = []
        watcher = self
        previous_create = Queue.create_job
        previous_enqueue = Queue.enqueue_job
        previous_perform = Job.perform
        # The class that defines it: ``BaseWorker`` in RQ 2 and later, ``Worker`` before.
        worker_cls: Any = next(c for c in Worker.__mro__ if "work" in c.__dict__)
        previous_work = worker_cls.work

        def create_job(queue: Any, *args: Any, **kwargs: Any) -> Any:
            job = previous_create(queue, *args, **kwargs)
            watcher._link(job)
            return job

        def enqueue_job(queue: Any, job: Any, *args: Any, **kwargs: Any) -> Any:
            watcher._link(job)  # jobs built with ``Job.create`` skip ``create_job``
            result = previous_enqueue(queue, job, *args, **kwargs)
            watcher._dispatched(job)
            return result

        def perform(job: Any) -> Any:
            return watcher._perform(job, previous_perform)

        def work(worker: Any, *args: Any, **kwargs: Any) -> Any:
            if not watcher._active:
                return previous_work(worker, *args, **kwargs)
            with internal():
                return previous_work(worker, *args, **kwargs)

        for cls, attr, previous, wrapper in (
            (Queue, "create_job", previous_create, create_job),
            (Queue, "enqueue_job", previous_enqueue, enqueue_job),
            (Job, "perform", previous_perform, perform),
            (worker_cls, "work", previous_work, work),
        ):
            setattr(cls, attr, wrapper)
            self._patches.append((cls, attr, previous, wrapper))

    def unregister(self) -> None:
        self._active = False
        # If something wrapped a method on top since, unwrapping would drop that: ours stays in
        # the chain and only delegates.
        for cls, attr, previous, wrapper in self._patches:
            if cls.__dict__.get(attr) is wrapper:
                setattr(cls, attr, previous)

    # ------------------------------------------------------------------ dispatch

    def _link(self, job: Any) -> None:
        """Store the current batch in ``job.meta``; a job that already has one keeps it."""
        if not self._active or is_internal():
            return
        batch = current_batch()
        if batch is not None and batch.recording and job.meta.get(PARENT_KEY) is None:
            job.meta[PARENT_KEY] = batch.id

    def _dispatched(self, job: Any) -> None:
        """A non-root entry in the dispatching batch, so the request detail lists its jobs."""
        if not self._active or is_internal():
            return
        content = self._content(job, "enqueue", _status(job))
        self.record(content, tags=self._tags(job, content["status"]))

    # ------------------------------------------------------------------ execution

    def _perform(self, job: Any, previous: Callable[[Any], Any]) -> Any:
        batch = current_batch()
        if not self._active or (batch and batch.meta.get(JOB_KEY) == job.id):
            return previous(job)  # inactive, or another instance already opened this job's batch
        # The job's own work is recorded, even inside the worker loop, which runs as internal.
        token = _internal.set(False)
        try:
            return self._run(job, previous)
        finally:
            _internal.reset(token)
            # A forked work horse ends with ``os._exit()``, which skips the ``atexit`` flush:
            # write the job's batch before returning to the worker. Only for an outermost job,
            # so a synchronous queue inside a request does not wait for other pending batches.
            if current_batch() is None:
                self.lw.flush(self.lw.config.flush_timeout)

    def _run(self, job: Any, previous: Callable[[Any], Any]) -> Any:
        started = time.perf_counter()
        error: BaseException | None = None
        with self.lw.batch("job", parent_id=job.meta.get(PARENT_KEY)) as batch:
            batch.meta[JOB_KEY] = job.id
            if not batch.recording:
                return previous(job)
            try:
                return previous(job)
            except BaseException as e:  # JobTimeoutException included
                error = e
                # Recorded here so it joins this batch: the worker catches it and nothing else
                # would see it.
                exceptions = self.lw.watchers.get("exception")
                if isinstance(exceptions, ExceptionWatcher) and isinstance(e, Exception):
                    exceptions.capture(e)
                raise
            finally:
                status = "finished" if error is None else "failed"
                content = self._content(job, "perform", status)
                content["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
                if error is not None:
                    cls = type(error)
                    content["error"] = {
                        "class": f"{cls.__module__}.{cls.__qualname__}",
                        "message": str(error),
                    }
                self.record(content, tags=self._tags(job, status), is_root=True, force=True)

    # ------------------------------------------------------------------ content

    def _content(self, job: Any, event: str, status: str) -> dict[str, Any]:
        """``event`` is ``enqueue`` (the dispatch) or ``perform`` (one execution)."""
        cfg = self.lw.config
        try:
            args, kwargs = list(job.args), dict(job.kwargs)
        except Exception:  # noqa: BLE001 - the function or its arguments can't be loaded here
            args, kwargs = [], {}
            redacted = True
        else:
            redacted = False
        shown_args = limit_payload(redact(args, cfg.hidden_params), cfg.max_body_kb)
        shown_kwargs = limit_payload(redact(kwargs, cfg.hidden_params), cfg.max_body_kb)
        # ``args`` and ``kwargs`` are the original arguments, ready to enqueue the job again, only
        # if nothing was masked, truncated or turned into a string to fit in JSON.
        redacted = redacted or jsonable(shown_args) != args or jsonable(shown_kwargs) != kwargs
        content = {
            "job_id": job.id,
            "function": job.func_name,
            "queue": job.origin,
            "event": event,
            "status": status,
            "args": shown_args,
            "kwargs": shown_kwargs,
            "args_redacted": redacted,
        }  # not ``job.description``: it repeats the arguments, unredacted
        if retry_of := job.meta.get(RETRY_KEY):
            content["retry_of"] = retry_of
        return content

    def _tags(self, job: Any, status: str) -> list[str]:
        tags = [f"rq_job:{job.id}", f"rq_queue:{job.origin}", f"rq_status:{status}"]
        if retry_of := job.meta.get(RETRY_KEY):
            tags.append(f"retry_of:{retry_of}")
        return tags


def _status(job: Any) -> str:
    """Status after ``enqueue_job``: ``queued``, ``deferred``, or the outcome of a sync run."""
    try:
        status = job.get_status(refresh=False)
    except Exception:  # noqa: BLE001 - a status that can't be read is still a dispatch
        return "queued"
    return str(getattr(status, "value", status) or "queued")
