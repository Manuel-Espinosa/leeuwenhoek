"""Dispatching a failed Celery task or RQ job again, from the panel, with its original arguments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import Entry
from .watchers.celery import RETRY_HEADER
from .watchers.rq import RETRY_KEY

if TYPE_CHECKING:
    from . import Leeuwenhoek

#: entry types that can be retried
TYPES = ("job", "rq")


class RetryError(Exception):
    """The entry can't be dispatched again; the message says why, for the panel."""


def is_failed_run(entry: Entry) -> bool:
    """A failed run of a job: not a dispatch, not a success. Only these get a Retry button."""
    c = entry.content
    if entry.type == "job":
        return entry.is_root and not c.get("dispatched") and c.get("status") == "FAILURE"
    if entry.type == "rq":
        return c.get("event") == "perform" and c.get("status") == "failed"
    return False


def allowed(lw: Leeuwenhoek) -> bool:
    """``LEEUWENHOEK_ALLOW_RETRY``; ``None`` (the default) means only in debug mode."""
    flag = lw.config.allow_retry
    if flag is None:
        return lw.app is not None and bool(lw.app.debug)
    return bool(flag)


def problem(lw: Leeuwenhoek, entry: Entry) -> str | None:
    """Why ``entry`` can't be retried, or ``None`` if it can."""
    if not is_failed_run(entry):
        return "Only failed runs can be retried."
    if not allowed(lw):
        return "Retrying jobs is disabled (LEEUWENHOEK_ALLOW_RETRY)."
    c = entry.content
    if c.get("args_redacted", True):
        return (
            "The original arguments were redacted, truncated or not plain JSON, "
            "so the job can't be dispatched again with them."
        )
    if entry.type == "rq" and rq_connection(lw) is None:
        return (
            "No Redis connection to enqueue the job: pass rq_connection= to init_app "
            "or set LEEUWENHOEK_RQ_REDIS_URL."
        )
    return None


def retry(lw: Leeuwenhoek, entry: Entry) -> str:
    """Dispatch ``entry`` again; returns the id of the new task or job.

    The new run records the failed entry's uuid as ``retry_of`` and gets the tag
    ``retry_of:<uuid>``. Raises :class:`RetryError` if it is not possible.
    """
    if (reason := problem(lw, entry)) is not None:
        raise RetryError(reason)
    if entry.type == "job":
        return _retry_celery(lw, entry)
    return _retry_rq(lw, entry)


def _retry_celery(lw: Leeuwenhoek, entry: Entry) -> str:
    import celery

    c = entry.content
    app = lw.celery or celery.current_app
    options: dict[str, Any] = {"headers": {RETRY_HEADER: entry.uuid}}
    if c.get("queue"):
        options["queue"] = c["queue"]
    task = app.tasks.get(c["name"])
    if task is not None:
        # Through the task when it is registered here: ``apply_async`` honours
        # ``task_always_eager`` and the task's own options, ``send_task`` only publishes.
        result = task.apply_async(c.get("args") or [], c.get("kwargs") or {}, **options)
    else:
        # A task defined only on the workers: published by name.
        result = app.send_task(c["name"], c.get("args") or [], c.get("kwargs") or {}, **options)
    return str(result.id)


def rq_connection(lw: Leeuwenhoek) -> Any:
    """The Redis connection to enqueue retries on: ``rq_connection=`` or ``RQ_REDIS_URL``."""
    if lw.rq_connection is not None:
        return lw.rq_connection
    url = lw.config.rq_redis_url
    if not url:
        return None
    from redis import Redis

    lw.rq_connection = Redis.from_url(url)
    return lw.rq_connection


def _retry_rq(lw: Leeuwenhoek, entry: Entry) -> str:
    from rq import Queue

    c = entry.content
    queue = Queue(c.get("queue") or "default", connection=rq_connection(lw))
    # ``args=``/``kwargs=`` keep the job's own arguments apart from ``enqueue``'s options.
    job = queue.enqueue(
        c["function"],
        args=c.get("args") or [],
        kwargs=c.get("kwargs") or {},
        meta={RETRY_KEY: entry.uuid},
    )
    return str(job.id)
