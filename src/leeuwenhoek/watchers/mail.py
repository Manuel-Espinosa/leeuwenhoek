"""Emails sent with Flask-Mail, through its ``email_dispatched`` signal."""

from __future__ import annotations

from email.utils import formataddr
from typing import Any

from flask import Flask

from ..filters import limit_payload, redact_headers
from .base import Watcher


class MailWatcher(Watcher):
    name = "mail"
    entry_type = "mail"
    label = "Mail"
    requires = "flask_mail"
    #: Flask-Mail's ``email_dispatched`` signal, once registered
    _signal: Any = None

    def register(self, app: Flask) -> None:
        # Imported here so this module loads without Flask-Mail installed.
        from flask_mail import email_dispatched

        self._signal = email_dispatched
        # Flask-Mail sends the signal with the app as sender, so only this app's mail is recorded.
        # The signal also fires with ``MAIL_SUPPRESS_SEND`` (the default when ``TESTING``).
        email_dispatched.connect(self._dispatched, sender=app)

    def unregister(self) -> None:
        if self._signal is not None:
            # From every sender: blinker only forgets the receiver when it is disconnected
            # from all of them (this bound method belongs to this watcher alone).
            self._signal.disconnect(self._dispatched)
            self._signal = None

    def signals(self) -> tuple[Any, ...]:
        return (self._signal,) if self._signal is not None else ()

    def _dispatched(self, app: Flask, message: Any = None, **extra: Any) -> None:
        if message is None:
            return
        cfg = self.lw.config
        attachments = getattr(message, "attachments", None) or []
        self.record(
            {
                "subject": message.subject,
                "sender": _address(message.sender),
                "to": [_address(a) for a in message.recipients or []],
                "cc": [_address(a) for a in message.cc or []],
                "bcc": [_address(a) for a in message.bcc or []],
                "reply_to": _address(message.reply_to) if message.reply_to else None,
                "html": limit_payload(message.html, cfg.max_body_kb) if message.html else None,
                "text": limit_payload(message.body, cfg.max_body_kb) if message.body else None,
                "attachments": [
                    {"filename": a.filename, "content_type": a.content_type} for a in attachments
                ],
                "headers": redact_headers(
                    (message.extra_headers or {}).items(), cfg.hidden_headers
                ),
            }
        )


def _address(value: Any) -> str | None:
    """``"Name <addr>"`` for a ``(name, addr)`` tuple, the string otherwise."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return formataddr((str(value[0]), str(value[1])))
    return str(value)
