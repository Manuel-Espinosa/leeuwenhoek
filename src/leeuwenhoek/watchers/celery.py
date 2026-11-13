"""Celery tasks: each run gets its own ``job`` batch, linked to the batch that dispatched it."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from flask import Flask

from ..context import Batch, is_internal
from ..filters import limit_payload, redact
from ..models import jsonable, new_id
from .base import Watcher
from .exception import ExceptionWatcher

#: message headers set on publish, read back by the worker (``task.request.<header>``)
PARENT_HEADER = "leeuwenhoek_parent_batch_id"
BATCH_HEADER = "leeuwenhoek_batch_id"
#: set by the panel's Retry button: uuid of the failed run entry this run retries
RETRY_HEADER = "leeuwenhoek_retry_of"

SIGNALS = ("before_task_publish", "task_prerun", "task_failure", "task_retry", "task_postrun")


class CeleryWatcher(Watcher):
    name = "celery"
    entry_type = "job"
    label = "Jobs"
    requires = "celery"

    def register(self, app: Flask) -> None:
        from celery import signals

        #: the Celery app passed to ``init_app``; ``None`` records the tasks of every Celery app
        self._celery = getattr(self.lw, "celery", None)
        # task id -> runs in progress (a stack: an eager task may run inside another run of it)
        self._running: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        # Celery signals are global: a ``dispatch_uid`` per watcher lets ``unregister`` remove
        # exactly its receivers, also when several instances are connected.
        self._uid = f"leeuwenhoek-celery-{id(self)}"
        self._signals = [getattr(signals, name) for name in SIGNALS]
        receivers = (self._publish, self._prerun, self._failure, self._retry, self._postrun)
        for signal, receiver in zip(self._signals, receivers):
            signal.connect(receiver, weak=False, dispatch_uid=self._uid)

    def unregister(self) -> None:
        for signal in getattr(self, "_signals", ()):
            signal.disconnect(dispatch_uid=self._uid)

    # ------------------------------------------------------------------ publish

    def _publish(
        self,
        sender: str | None = None,
        body: Any = None,
        headers: dict[str, Any] | None = None,
        routing_key: str | None = None,
        **_: Any,
    ) -> None:
        """Tag the message with the current batch; runs where the task is dispatched.

        Not fired for eager tasks (``task_always_eager``, ``apply()``): ``_prerun`` handles them.
        """
        batch = self.lw.current()
        if headers is None or is_internal() or batch is None or not batch.recording:
            return
        task_id = headers.get("id")
        if batch.kind == "job" and batch.meta.get("task_id") == task_id:
            # ``self.retry()``: the next attempt is linked to the batch that dispatched the first
            headers[PARENT_HEADER] = batch.parent_id
            headers[BATCH_HEADER] = new_id()
            return
        job_batch_id = new_id()
        headers[PARENT_HEADER] = batch.id
        headers[BATCH_HEADER] = job_batch_id
        args, kwargs = _body_args(body)
        self._record_dispatch(
            name=sender or headers.get("task"),
            task_id=task_id,
            args=args,
            kwargs=kwargs,
            queue=routing_key,
            job_batch_id=job_batch_id,
            eta=headers.get("eta"),
        )

    def _record_dispatch(
        self,
        *,
        name: Any,
        task_id: Any,
        args: Any,
        kwargs: Any,
        queue: Any,
        job_batch_id: str,
        eta: Any = None,
    ) -> None:
        """A ``job`` entry in the dispatching batch, so its page links to the job's batch."""
        content = {
            "name": name,
            "task_id": task_id,
            "status": "DISPATCHED",
            "dispatched": True,
            "queue": queue,
            "eta": eta,
            "job_batch_id": job_batch_id,
            **self._arguments(args, kwargs),
        }
        self.record(content, tags=[f"task:{name}"], force=True)

    # ------------------------------------------------------------------ worker

    def _mine(self, task: Any) -> bool:
        return task is not None and (self._celery is None or task.app is self._celery)

    def _prerun(
        self,
        task_id: str | None = None,
        task: Any = None,
        args: Any = None,
        kwargs: Any = None,
        **_: Any,
    ) -> None:
        if task_id is None or not self._mine(task) or is_internal():
            return
        request = task.request
        parent_id = getattr(request, PARENT_HEADER, None)
        batch_id = getattr(request, BATCH_HEADER, None)
        if (request.delivery_info or {}).get("redelivered"):
            batch_id = None  # the first delivery may have used it already
        current = self.lw.current()
        if parent_id is None and current is not None and current.recording:
            # No headers: the task runs in this process (eager mode or ``apply()``), inside the
            # batch that dispatched it, which is therefore its parent.
            if current.kind == "job" and current.meta.get("task_id") == task_id:
                parent_id = current.parent_id
            else:
                parent_id = current.id
            batch_id = new_id()
            if not request.retries:  # eager retries are not dispatched again
                self._record_dispatch(
                    name=task.name,
                    task_id=task_id,
                    args=args,
                    kwargs=kwargs,
                    queue=self._queue(task),
                    job_batch_id=batch_id,
                )
        batch, token = self.lw.open_batch("job", parent_id=parent_id)
        if batch_id:
            batch.id = batch_id  # nothing has been recorded in it yet
        batch.meta["task_id"] = task_id
        run = {
            "batch": batch,
            "token": token,
            "started": time.perf_counter(),
            "retry_of": _header(request, RETRY_HEADER),
        }
        with self._lock:
            self._running.setdefault(task_id, []).append(run)

    def _run(self, task_id: str | None) -> dict[str, Any] | None:
        with self._lock:
            stack = self._running.get(task_id or "")
            return stack[-1] if stack else None

    def _failure(self, task_id: str | None = None, exception: Any = None, **_: Any) -> None:
        run = self._run(task_id)
        if run is None or not isinstance(exception, BaseException):
            return
        run["exception"] = exception
        # Recorded here, while the job's batch is current, so the exception joins it. It is
        # marked, so a request that re-raises it (eager mode) does not record it again.
        exceptions = self.lw.watchers.get("exception")
        if isinstance(exceptions, ExceptionWatcher) and isinstance(exception, Exception):
            exceptions.capture(exception)

    def _retry(self, request: Any = None, reason: Any = None, **_: Any) -> None:
        run = self._run(getattr(request, "id", None))
        if run is not None:
            # ``reason`` is the ``Retry`` raised by ``self.retry()``, wrapping ``exc=`` if given
            cause = getattr(reason, "exc", None) or reason
            run["retry_reason"] = str(cause) if cause is not None else None

    def _postrun(
        self,
        task_id: str | None = None,
        task: Any = None,
        args: Any = None,
        kwargs: Any = None,
        retval: Any = None,
        state: str | None = None,
        **_: Any,
    ) -> None:
        with self._lock:
            stack = self._running.get(task_id or "")
            if not stack:
                return
            run = stack.pop()
            if not stack:
                del self._running[task_id or ""]
        batch: Batch = run["batch"]
        exc: BaseException | None = run.get("exception")
        try:
            if batch.recording:
                self._record_run(task, task_id, args, kwargs, retval, state, run)
        finally:
            self.lw.close_batch(batch, run["token"], exc)

    def _record_run(
        self,
        task: Any,
        task_id: str | None,
        args: Any,
        kwargs: Any,
        retval: Any,
        state: str | None,
        run: dict[str, Any],
    ) -> None:
        cfg = self.lw.config
        request = task.request
        retries = request.retries or 0
        exc = run.get("exception")
        content = {
            "name": task.name,
            "task_id": task_id,
            "status": state,
            "queue": self._queue(task),
            "attempt": retries + 1,
            "retries": retries,
            "max_retries": getattr(task, "max_retries", None),
            "eager": bool(request.is_eager),
            "worker": request.hostname,
            "duration_ms": round((time.perf_counter() - run["started"]) * 1000, 2),
            **self._arguments(args, kwargs),
            "result": (
                limit_payload(redact(jsonable(retval), cfg.hidden_params), cfg.max_body_kb)
                if state == "SUCCESS"
                else None
            ),
            "exception": (
                {"class": f"{type(exc).__module__}.{type(exc).__qualname__}", "message": str(exc)}
                if exc is not None
                else None
            ),
            "retry_reason": run.get("retry_reason"),
        }
        tags = [f"task:{task.name}", f"state:{state}"]
        if retry_of := run.get("retry_of"):
            content["retry_of"] = retry_of
            tags.append(f"retry_of:{retry_of}")
        self.record(content, tags=tags, is_root=True, force=True)

    # ------------------------------------------------------------------ helpers

    def _arguments(self, args: Any, kwargs: Any) -> dict[str, Any]:
        """Redacted and truncated ``args``/``kwargs``, plus ``args_redacted``.

        ``args_redacted`` is false only when the stored values are exactly the originals (nothing
        redacted, truncated or turned into a ``repr``), so the task can be dispatched again.
        """
        cfg = self.lw.config
        original = {"args": list(args or ()), "kwargs": dict(kwargs or {})}
        shown = {
            key: limit_payload(redact(jsonable(value), cfg.hidden_params), cfg.max_body_kb)
            for key, value in original.items()
        }
        return {**shown, "args_redacted": shown != jsonable(original) or not _json_exact(original)}

    def _queue(self, task: Any) -> str | None:
        info = task.request.delivery_info or {}
        return (
            info.get("routing_key")
            or getattr(task, "queue", None)
            or task.app.conf.task_default_queue
        )


def _header(request: Any, name: str) -> Any:
    """A custom message header: an attribute on a worker, in ``request.headers`` when eager."""
    value = getattr(request, name, None)
    if value is None:
        value = (getattr(request, "headers", None) or {}).get(name)
    return value


def _body_args(body: Any) -> tuple[Any, Any]:
    """``args`` and ``kwargs`` of a message body (protocol 2: ``(args, kwargs, embed)``)."""
    if isinstance(body, (tuple, list)) and len(body) >= 2:
        return body[0], body[1]
    if isinstance(body, dict):  # protocol 1
        return body.get("args"), body.get("kwargs")
    return None, None


def _json_exact(value: Any) -> bool:
    """Whether ``value`` survives a JSON round trip unchanged (tuples aside)."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True
