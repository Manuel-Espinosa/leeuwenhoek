"""Redis commands sent with redis-py (``redis.Redis`` and ``redis.asyncio.Redis``), and pipelines."""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from flask import Flask

from ..context import is_internal
from ..filters import MASK
from .base import Watcher
from .cache import hidden_key
from .http_client import _Patch

logger = logging.getLogger("leeuwenhoek")

#: Arguments kept per command; the rest are counted in ``args_omitted``.
MAX_ARGS = 20
#: Characters kept per argument.
MAX_ARG_CHARS = 200
#: Commands kept per pipeline; the rest are counted in ``commands_omitted``.
MAX_PIPELINE_COMMANDS = 50

#: Set while a patched method runs, so a command sent from inside another one (or the commands
#: of a pipeline, should redis-py ever route them through ``execute_command``) is not recorded
#: a second time.
_inside: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "leeuwenhoek_redis_inside", default=False
)


class RedisWatcher(Watcher):
    name = "redis"
    entry_type = "redis"
    label = "Redis"
    requires = "redis"

    def __init__(self, lw) -> None:
        super().__init__(lw)
        self._patches: list[_Patch] = []

    def register(self, app: Flask) -> None:
        # The client classes are patched, so every client is covered, including those created
        # before ``init_app`` and inside other libraries (Flask-Caching's RedisCache, RQ).
        # ``Pipeline`` overrides ``execute_command`` to queue commands, so queued commands are
        # never seen here; they are recorded together when the pipeline runs ``execute``.
        import redis.asyncio.client as async_client
        import redis.client as sync_client

        self._patches = [
            _Patch(sync_client.Redis, "execute_command", self._wrap_command),
            # commands sent right away by a pipeline in WATCH mode
            _Patch(sync_client.Pipeline, "immediate_execute_command", self._wrap_command),
            _Patch(sync_client.Pipeline, "execute", self._wrap_pipeline),
            _Patch(async_client.Redis, "execute_command", self._wrap_command_async),
            _Patch(async_client.Pipeline, "immediate_execute_command", self._wrap_command_async),
            _Patch(async_client.Pipeline, "execute", self._wrap_pipeline_async),
        ]

    def unregister(self) -> None:
        for patch in reversed(self._patches):
            patch.undo()
        self._patches = []

    # ------------------------------------------------------------------ wrappers

    def _wrap_command(self, patch: _Patch) -> Callable[..., Any]:
        def execute_command(client: Any, *args: Any, **options: Any) -> Any:
            if not patch.active or not self._wanted():
                return patch.original(client, *args, **options)
            token = _inside.set(True)
            started = time.perf_counter()
            try:
                result = patch.original(client, *args, **options)
            except Exception as exc:
                self._safe_record(client, args, None, started, exc, False)
                raise
            finally:
                _inside.reset(token)
            self._safe_record(client, args, None, started, None, False)
            return result

        return execute_command

    def _wrap_command_async(self, patch: _Patch) -> Callable[..., Any]:
        async def execute_command(client: Any, *args: Any, **options: Any) -> Any:
            if not patch.active or not self._wanted():
                return await patch.original(client, *args, **options)
            token = _inside.set(True)
            started = time.perf_counter()
            try:
                result = await patch.original(client, *args, **options)
            except Exception as exc:
                self._safe_record(client, args, None, started, exc, True)
                raise
            finally:
                _inside.reset(token)
            self._safe_record(client, args, None, started, None, True)
            return result

        return execute_command

    def _wrap_pipeline(self, patch: _Patch) -> Callable[..., Any]:
        def execute(pipe: Any, *args: Any, **kwargs: Any) -> Any:
            if not patch.active or not self._wanted() or not pipe.command_stack:
                return patch.original(pipe, *args, **kwargs)
            stack = _stack(pipe)  # ``execute`` empties the stack
            token = _inside.set(True)
            started = time.perf_counter()
            try:
                result = patch.original(pipe, *args, **kwargs)
            except Exception as exc:
                self._safe_record(pipe, None, stack, started, exc, False)
                raise
            finally:
                _inside.reset(token)
            self._safe_record(pipe, None, stack, started, None, False, result)
            return result

        return execute

    def _wrap_pipeline_async(self, patch: _Patch) -> Callable[..., Any]:
        async def execute(pipe: Any, *args: Any, **kwargs: Any) -> Any:
            if not patch.active or not self._wanted() or not pipe.command_stack:
                return await patch.original(pipe, *args, **kwargs)
            stack = _stack(pipe)
            token = _inside.set(True)
            started = time.perf_counter()
            try:
                result = await patch.original(pipe, *args, **kwargs)
            except Exception as exc:
                self._safe_record(pipe, None, stack, started, exc, True)
                raise
            finally:
                _inside.reset(token)
            self._safe_record(pipe, None, stack, started, None, True, result)
            return result

        return execute

    def _wanted(self) -> bool:
        """Skip nested commands, internal calls and batches not being recorded."""
        if _inside.get() or is_internal():
            return False
        batch = self.lw.current()
        return batch is None or batch.recording

    # ------------------------------------------------------------------ capture

    def _safe_record(
        self,
        client: Any,
        args: Sequence[Any] | None,
        stack: list[Sequence[Any]] | None,
        started: float,
        error: BaseException | None,
        is_async: bool,
        results: Any = None,
    ) -> None:
        duration = (time.perf_counter() - started) * 1000
        try:
            self._record(client, args, stack, duration, error, is_async, results)
        except Exception:  # recording must never break the app's Redis call
            logger.warning("leeuwenhoek: could not record a Redis command", exc_info=True)

    def _record(
        self,
        client: Any,
        args: Sequence[Any] | None,
        stack: list[Sequence[Any]] | None,
        duration: float,
        error: BaseException | None,
        is_async: bool,
        results: Any,
    ) -> None:
        hidden = self.lw.config.hidden_params
        content: dict[str, Any] = {}
        if stack is None:
            content.update(_command(args or (), hidden))
        else:
            commands = [_command(cmd, hidden) for cmd in stack[:MAX_PIPELINE_COMMANDS]]
            if isinstance(results, list):
                for cmd, result in zip(commands, results):
                    if isinstance(result, Exception):
                        cmd["error"] = _error(result)
            content.update(
                command="PIPELINE",
                args=[],
                transaction=bool(getattr(client, "transaction", False)),
                count=len(stack),
                commands=commands,
            )
            if len(stack) > MAX_PIPELINE_COMMANDS:
                content["commands_omitted"] = len(stack) - MAX_PIPELINE_COMMANDS
        content.update(_server(client))
        content["async"] = is_async
        content["duration_ms"] = round(duration, 3)
        content["error"] = _error(error) if error is not None else None
        tags = ["failed"] if error is not None else []
        self.record(content, tags=tags)


def _stack(pipe: Any) -> list[Sequence[Any]]:
    """The arguments of each queued command (redis-py stores ``(args, options)`` pairs)."""
    return [
        item[0] if isinstance(item, tuple) and len(item) == 2 else item
        for item in pipe.command_stack
    ]


def _command(args: Sequence[Any], hidden: Iterable[str]) -> dict[str, Any]:
    """Command name and arguments, redacted, decoded and truncated."""
    if not args:
        return {"command": "", "args": []}
    name = _text(args[0]).upper()
    rest = [_text(a) for a in args[1:]]
    # Multi-word commands (``CONFIG SET``, ``ACL SETUSER``, ``CLIENT SETNAME``) arrive either as
    # one argument or as two.
    if " " in name:
        name, _, sub = name.partition(" ")
        rest.insert(0, sub)
    rest = _redact(name, rest, list(hidden))
    content: dict[str, Any] = {
        "command": name,
        "args": [_truncate(a) for a in rest[:MAX_ARGS]],
    }
    if len(rest) > MAX_ARGS:
        content["args_omitted"] = len(rest) - MAX_ARGS
    return content


def _redact(name: str, args: list[str], hidden: list[str]) -> list[str]:
    """Mask passwords, and values stored under keys or fields with a hidden name.

    ``AUTH`` arguments are always masked, as are the password of ``HELLO ... AUTH`` and
    ``MIGRATE ... AUTH``/``AUTH2``, ``CONFIG SET requirepass``/``masterauth`` and the password
    rules of ``ACL SETUSER``. When the key (the first argument) contains one of the hidden names
    (``user:1:token``) every argument after it is masked, and any later argument that contains
    one (a hash field such as ``password``) masks the argument that follows it.
    """
    if name == "AUTH":
        return [MASK for _ in args]
    out = list(args)
    upper = [a.upper() for a in args]
    if name in ("HELLO", "MIGRATE"):
        for i, word in enumerate(upper):
            if word == "AUTH" and i + 1 < len(out):
                # HELLO AUTH <username> <password>; MIGRATE AUTH <password>
                j = i + 2 if name == "HELLO" else i + 1
                if j < len(out):
                    out[j] = MASK
            elif word == "AUTH2" and i + 2 < len(out):
                out[i + 2] = MASK
        return out
    if name == "CONFIG" and upper[:1] == ["SET"]:
        for i in range(1, len(out) - 1, 2):
            if upper[i] in ("REQUIREPASS", "MASTERAUTH", "MASTERUSER") or hidden_key(
                out[i], hidden
            ):
                out[i + 1] = MASK
        return out
    if name == "ACL" and upper[:1] == ["SETUSER"]:
        return [MASK if a[:1] in (">", "<", "#", "!") else a for a in out]
    if out and hidden_key(out[0], hidden):
        return out[:1] + [MASK for _ in out[1:]]
    for i in range(1, len(out) - 1):
        if out[i] != MASK and hidden_key(out[i], hidden):
            out[i + 1] = MASK
    return out


def _text(value: Any) -> str:
    """An argument as text: bytes decoded as UTF-8, or a placeholder when they are binary."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return f"[binary: {len(value)} bytes]"
    return str(value)


def _truncate(text: str) -> str:
    if len(text) <= MAX_ARG_CHARS:
        return text
    return text[:MAX_ARG_CHARS] + f"… [truncated: {len(text)} characters]"


def _server(client: Any) -> dict[str, Any]:
    """Host (``host:port`` or the unix socket path) and database number of the client."""
    try:
        kwargs = client.connection_pool.connection_kwargs
    except AttributeError:  # a client without a pool (a single connection, a cluster)
        return {"host": None, "db": None}
    host = kwargs.get("path") or (
        f"{kwargs['host']}:{kwargs.get('port', 6379)}" if kwargs.get("host") else None
    )
    return {"host": host, "db": kwargs.get("db", 0)}


def _error(error: BaseException) -> dict[str, str]:
    return {
        "class": f"{type(error).__module__}.{type(error).__qualname__}",
        "message": str(error),
    }
