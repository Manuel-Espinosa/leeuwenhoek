"""Blinker signals: the Flask signals in ``LEEUWENHOEK_SIGNALS`` and those added with ``watch_signal``."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from blinker import ANY, Signal
from flask import Flask, current_app, has_app_context, has_request_context, request
from flask import signals as flask_signals

from ..context import current_batch, is_internal
from ..filters import limit_payload, redact
from ..models import jsonable
from .base import Watcher

log = logging.getLogger("leeuwenhoek")

#: longest ``repr`` of a sender that is kept
MAX_SENDER = 200


class EventWatcher(Watcher):
    name = "event"
    entry_type = "event"
    label = "Events"

    def register(self, app: Flask) -> None:
        self._app = app
        #: ``id(signal) -> (signal, receiver)``; the signal is kept so its id stays unique
        self._receivers: dict[int, tuple[Signal, Callable[..., None]]] = {}
        for name in self.lw.config.signals or ():
            signal = getattr(flask_signals, name, None)
            if not isinstance(signal, Signal):
                log.warning("Unknown Flask signal: %s", name)
                continue
            # Flask sends its signals with the app as sender: only this app's are recorded.
            self._connect(signal, name, sender=app)
        for signal, name in self.lw._signals:
            self.watch(signal, name)

    def unregister(self) -> None:
        for signal, receiver in self._receivers.values():
            # From every sender: blinker only forgets the receiver when it is disconnected
            # from all of them.
            signal.disconnect(receiver)
        self._receivers.clear()

    def watch(self, signal: Signal, name: str | None = None) -> None:
        """Record every send of ``signal``, from any sender. Does nothing if already watched."""
        if id(signal) not in self._receivers:
            self._connect(signal, name or signal_name(signal), sender=ANY)

    def _connect(self, signal: Signal, name: str, sender: Any) -> None:
        if id(signal) in self._receivers:
            return

        def receiver(sender: Any, **kwargs: Any) -> None:
            self._received(signal, name, sender, kwargs)

        # A strong reference: the closure lives only as long as the connection.
        signal.connect(receiver, sender=sender, weak=False)
        self._receivers[id(signal)] = (signal, receiver)

    def _received(self, signal: Signal, name: str, sender: Any, kwargs: dict[str, Any]) -> None:
        if is_internal() or self._skipped(signal):
            return
        try:
            cfg = self.lw.config
            content = {
                "signal": name,
                "sender": describe(sender),
                "sender_type": type_name(sender),
                "kwargs": limit_payload(
                    redact({str(k): safe(v) for k, v in kwargs.items()}, cfg.hidden_params),
                    cfg.max_body_kb,
                ),
            }
            self.record(content, tags=[f"event:{name}"])
        except Exception:  # recording must never break the code that sent the signal
            log.exception("Could not record the signal %s", name)

    def _skipped(self, signal: Signal) -> bool:
        """Not recorded: a batch left out, the panel, another app, or a signal another watcher records."""
        batch = current_batch()
        if batch is not None:
            if not batch.recording:  # the panel, an ignored path, paused, sampled out
                return True
        elif has_request_context() and self.lw.skips_path(request.path):
            # Sent before the request batch opens or after it closes (``request_started``).
            return True
        if has_app_context() and current_app._get_current_object() is not self._app:  # type: ignore[attr-defined]
            return True
        return any(signal in w.signals() for w in self.lw.watchers.values() if w is not self)


def signal_name(signal: Signal) -> str:
    """The name of a named signal (``blinker.signal("order-placed")``), else its ``repr``."""
    return getattr(signal, "name", None) or repr(signal)


def describe(sender: Any) -> str | None:
    """A short text for the sender: the qualified name of a class, else a truncated ``repr``."""
    if sender is None:
        return None
    if isinstance(sender, type):
        return f"{sender.__module__}.{sender.__qualname__}"
    try:
        text = repr(sender)
    except Exception:  # noqa: BLE001 - any repr may raise
        return f"<{type_name(sender)}>"
    return text if len(text) <= MAX_SENDER else text[: MAX_SENDER - 1] + "…"


def type_name(value: Any) -> str | None:
    if value is None:
        return None
    cls = value if isinstance(value, type) else type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def safe(value: Any) -> Any:
    """``value`` as JSON; anything that cannot even be turned into a ``repr`` is named by type."""
    try:
        return jsonable(value)
    except Exception:  # noqa: BLE001 - any repr may raise
        return f"<{type_name(value)}>"
