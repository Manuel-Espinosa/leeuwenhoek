"""``flask`` CLI commands: each one gets its own batch, with its arguments, exit code and time."""

from __future__ import annotations

import time
from collections.abc import Callable
from fnmatch import fnmatch
from typing import Any

import click
from flask import Flask

from ..context import current_batch, is_internal
from ..filters import limit_payload, redact
from .base import Watcher
from .exception import ExceptionWatcher

#: never recorded: Leeuwenhoek's own commands, the dev server and the interactive shell
ALWAYS_IGNORED = ("leeuwenhoek", "run", "shell")

Invoke = Callable[[click.Command, click.Context], Any]


class CommandWatcher(Watcher):
    name = "command"
    entry_type = "command"
    label = "Commands"

    def register(self, app: Flask) -> None:
        self._app = app
        self._active = True
        # ``click.Command.invoke`` is wrapped for every command, and the ones of ``app.cli`` are
        # picked when they run, so groups and commands registered after ``init_app`` count too.
        # Groups dispatch to their subcommands through the same method, which is where the
        # batch is opened. Chained, never replaced, like the exception watcher's hooks.
        self._previous: Invoke = click.Command.invoke
        watcher = self

        def invoke(command: click.Command, ctx: click.Context) -> Any:
            return watcher._invoke(command, ctx)

        self._wrapper = invoke
        click.Command.invoke = invoke  # type: ignore[method-assign,assignment]

    def unregister(self) -> None:
        self._active = False
        # If something wrapped it on top since, unwrapping would drop that: ours stays in the
        # chain and only delegates.
        if click.Command.__dict__.get("invoke") is self._wrapper:
            click.Command.invoke = self._previous  # type: ignore[method-assign,assignment]

    def _invoke(self, command: click.Command, ctx: click.Context) -> Any:
        name = self._command_name(command)
        if name is None:
            return self._previous(command, ctx)
        started = time.perf_counter()
        with self.lw.batch("command", skip=self._ignored(name)) as batch:
            if not batch.recording:
                return self._previous(command, ctx)
            exit_code = 0
            try:
                return self._previous(command, ctx)
            except click.exceptions.Exit as e:
                exit_code = e.exit_code
                raise
            except SystemExit as e:
                exit_code = exit_code_of(e.code)
                raise
            except click.ClickException as e:  # an error message for the user, not a bug
                exit_code = e.exit_code
                raise
            except click.Abort:
                exit_code = 1
                raise
            except Exception as e:
                exit_code = 1
                # Recorded here so it joins this batch; it is marked, so when it then reaches
                # ``sys.excepthook`` it is not recorded again.
                exceptions = self.lw.watchers.get("exception")
                if isinstance(exceptions, ExceptionWatcher):
                    exceptions.capture(e)
                raise
            except BaseException:  # KeyboardInterrupt: Click aborts with exit code 1
                exit_code = 1
                raise
            finally:
                self._record(name, command, ctx, exit_code, started)

    def _command_name(self, command: click.Command) -> str | None:
        """Full name (``"group sub"``) of a leaf command of ``app.cli``; ``None`` otherwise."""
        if not self._active or is_internal() or isinstance(command, click.Group):
            return None
        batch = current_batch()
        if batch is not None and batch.kind == "command":  # invoked from another command
            return None
        path = find(self._app.cli, command)
        return " ".join(path) if path else None

    def _ignored(self, name: str) -> bool:
        patterns = [*ALWAYS_IGNORED, *(self.lw.config.ignore_commands or ())]
        return any(name == p or name.startswith(p + " ") or fnmatch(name, p) for p in patterns)

    def _record(
        self,
        name: str,
        command: click.Command,
        ctx: click.Context,
        exit_code: int,
        started: float,
    ) -> None:
        cfg = self.lw.config
        arguments: dict[str, Any] = {}
        options: dict[str, Any] = {}
        for param in command.params:
            if param.name is None or param.name not in ctx.params:
                continue
            target = arguments if isinstance(param, click.Argument) else options
            target[param.name] = ctx.params[param.name]
        if ctx.args:  # extra arguments of commands that allow them
            arguments["*"] = list(ctx.args)
        content = {
            "name": name,
            "arguments": limit_payload(redact(arguments, cfg.hidden_params), cfg.max_body_kb),
            "options": limit_payload(redact(options, cfg.hidden_params), cfg.max_body_kb),
            "exit_code": exit_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        tags = [f"command:{name}", f"exit_code:{exit_code}"]
        self.record(content, tags=tags, is_root=True, force=True)


def find(group: click.Group, command: click.Command) -> list[str] | None:
    """Names leading from ``group`` to ``command`` through nested groups, if it is in there."""
    for name, cmd in group.commands.items():
        if cmd is command:
            return [name]
        if isinstance(cmd, click.Group) and (path := find(cmd, command)):
            return [name, *path]
    return None


def exit_code_of(code: Any) -> int:
    """Exit status of ``SystemExit(code)``: ``None`` is 0, a message is 1."""
    if code is None:
        return 0
    return code if isinstance(code, int) else 1
