"""Jinja templates rendered through Flask: name, context keys and duration."""

from __future__ import annotations

import contextvars
import time
from typing import Any

from flask import Flask, before_render_template, template_rendered
from jinja2 import Template

from .base import Watcher

#: prefix of Leeuwenhoek's own templates, which are never recorded
OWN_PREFIX = "leeuwenhoek/"
#: keys Flask adds to every template context (``update_template_context``)
FLASK_DEFAULTS = frozenset({"g", "request", "session"})
#: renders in flight kept per context; bounds what renders that raised (and so never sent
#: ``template_rendered``) leave behind
MAX_DEPTH = 32

# Renders in flight, innermost last: ``(id of the context dict, start)``. Only the id is kept, so
# a render that raised holds no reference to its context. A tuple, never mutated in place, so
# each thread or task sees only its own renders.
_stack: contextvars.ContextVar[tuple[tuple[int, float], ...]] = contextvars.ContextVar(
    "leeuwenhoek_templates", default=()
)


class TemplateWatcher(Watcher):
    name = "template"
    entry_type = "template"
    label = "Templates"

    def register(self, app: Flask) -> None:
        self._app = app
        before_render_template.connect(self._before, app)
        template_rendered.connect(self._rendered, app)

    def unregister(self) -> None:
        before_render_template.disconnect(self._before, self._app)
        template_rendered.disconnect(self._rendered, self._app)

    def _before(self, sender: Flask, template: Template, context: dict[str, Any], **_: Any) -> None:
        if is_own(template):
            return
        stack = _stack.get()[-(MAX_DEPTH - 1) :]
        _stack.set((*stack, (id(context), time.perf_counter())))

    def _rendered(
        self, sender: Flask, template: Template, context: dict[str, Any], **_: Any
    ) -> None:
        if is_own(template):
            return
        # Signals fire once per top-level render (``render_template`` and friends), not for
        # ``{% include %}`` or ``{% extends %}``. A render may still call another one (from a
        # context processor or a filter), so pair by the context dict, which both signals share.
        # The innermost match is the right one: this dict is alive, so a stale entry with the
        # same (reused) id can only sit below it. Anything above the match is a nested render
        # that raised and never finished.
        started = None
        stack = _stack.get()
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == id(context):
                started = stack[i][1]
                _stack.set(stack[:i])
                break
        duration = (time.perf_counter() - started) * 1000 if started is not None else None
        self.record(
            {
                "name": template.name,
                "file": template.filename,
                "context_keys": sorted(str(k) for k in context if k not in FLASK_DEFAULTS),
                "duration_ms": round(duration, 3) if duration is not None else None,
            },
            tags=[f"template:{template.name}"] if template.name else None,
        )


def is_own(template: Template) -> bool:
    return (template.name or "").startswith(OWN_PREFIX)
