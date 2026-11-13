"""Outgoing HTTP calls made with ``requests`` or ``httpx`` (sync and async clients)."""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from flask import Flask

from ..context import is_internal
from ..filters import limit_payload, redact, redact_headers, redact_url
from .base import Watcher, _installed

logger = logging.getLogger("leeuwenhoek")

#: Set while a patched ``send`` runs, so the hops of a redirect (``requests`` sends each one
#: through ``Session.send`` again) are folded into the outermost call.
_sending: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "leeuwenhoek_http_sending", default=False
)


class _Patch:
    """Replace ``owner.attr`` with a wrapper; ``undo`` puts the original back.

    If something else patched the same attribute on top of ours since, restoring would drop it,
    so ours stays in the chain and only delegates (``active`` is False). When a patch of ours on
    top is undone later, it skips the inactive ones below it, so once every Leeuwenhoek instance
    is closed the original method is back.
    """

    def __init__(self, owner: type, attr: str, make: Callable[[_Patch], Callable[..., Any]]):
        self.owner = owner
        self.attr = attr
        self.original: Callable[..., Any] = getattr(owner, attr)
        self.active = True
        self.wrapper = functools.wraps(self.original)(make(self))
        self.wrapper._leeuwenhoek_patch = self  # type: ignore[attr-defined]
        setattr(owner, attr, self.wrapper)

    def undo(self) -> None:
        self.active = False
        if self.owner.__dict__.get(self.attr) is not self.wrapper:
            return
        target = self.original
        while (below := getattr(target, "_leeuwenhoek_patch", None)) and not below.active:
            target = below.original
        setattr(self.owner, self.attr, target)


class HttpClientWatcher(Watcher):
    name = "http"
    entry_type = "http"
    label = "HTTP client"
    #: either library is enough (see ``missing``)
    requires = ("requests", "httpx")

    @classmethod
    def missing(cls) -> list[str]:
        """Nothing is missing as long as one of the supported clients is installed."""
        modules = [m for m in cls.requires or () if m]
        return [] if any(_installed(m) for m in modules) else modules

    def __init__(self, lw) -> None:
        super().__init__(lw)
        self._patches: list[_Patch] = []

    def register(self, app: Flask) -> None:
        # ``send`` is patched on the client classes rather than through httpx event hooks or a
        # transport, because those are per-client options: patching the class also catches
        # clients created before ``init_app`` and by third-party libraries, and it is the only
        # place that sees the duration, the error if the call raises, and the final response
        # after redirects.
        if _installed("requests"):
            import requests

            self._patches.append(_Patch(requests.Session, "send", self._wrap_requests))
        if _installed("httpx"):
            import httpx

            self._patches.append(_Patch(httpx.Client, "send", self._wrap_httpx))
            self._patches.append(_Patch(httpx.AsyncClient, "send", self._wrap_httpx_async))

    def unregister(self) -> None:
        for patch in reversed(self._patches):
            patch.undo()
        self._patches = []

    # ------------------------------------------------------------------ wrappers

    def _wrap_requests(self, patch: _Patch) -> Callable[..., Any]:
        def send(session: Any, request: Any, **kwargs: Any) -> Any:
            if not patch.active or not self._wanted():
                return patch.original(session, request, **kwargs)
            token = _sending.set(True)
            started = time.perf_counter()
            try:
                response = patch.original(session, request, **kwargs)
            except Exception as exc:
                self._safe_record("requests", request, None, started, exc, False)
                raise
            finally:
                _sending.reset(token)
            self._safe_record("requests", request, response, started, None, kwargs.get("stream"))
            return response

        return send

    def _wrap_httpx(self, patch: _Patch) -> Callable[..., Any]:
        def send(client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            if not patch.active or not self._wanted():
                return patch.original(client, request, *args, **kwargs)
            token = _sending.set(True)
            started = time.perf_counter()
            try:
                response = patch.original(client, request, *args, **kwargs)
            except Exception as exc:
                self._safe_record("httpx", request, None, started, exc, False)
                raise
            finally:
                _sending.reset(token)
            self._safe_record("httpx", request, response, started, None, kwargs.get("stream"))
            return response

        return send

    def _wrap_httpx_async(self, patch: _Patch) -> Callable[..., Any]:
        async def send(client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            if not patch.active or not self._wanted():
                return await patch.original(client, request, *args, **kwargs)
            token = _sending.set(True)
            started = time.perf_counter()
            try:
                response = await patch.original(client, request, *args, **kwargs)
            except Exception as exc:
                self._safe_record("httpx", request, None, started, exc, False)
                raise
            finally:
                _sending.reset(token)
            self._safe_record("httpx", request, response, started, None, kwargs.get("stream"))
            return response

        return send

    def _wanted(self) -> bool:
        """Skip nested sends (redirect hops), internal calls and batches not being recorded."""
        if _sending.get() or is_internal():
            return False
        batch = self.lw.current()
        return batch is None or batch.recording

    # ------------------------------------------------------------------ capture

    def _safe_record(
        self,
        library: str,
        request: Any,
        response: Any,
        started: float,
        error: BaseException | None,
        streamed: bool | None,
    ) -> None:
        duration = (time.perf_counter() - started) * 1000
        try:
            self._record(library, request, response, duration, error, bool(streamed))
        except Exception:  # recording must never break the app's HTTP call
            logger.warning("leeuwenhoek: could not record an HTTP call", exc_info=True)

    def _record(
        self,
        library: str,
        request: Any,
        response: Any,
        duration: float,
        error: BaseException | None,
        streamed: bool,
    ) -> None:
        cfg = self.lw.config
        hidden = cfg.hidden_params
        url = str(request.url)
        host = urlsplit(url).hostname or ""
        content: dict[str, Any] = {
            "library": library,
            "method": request.method,
            "url": redact_url(url, hidden),
            "host": host,
            "request_headers": redact_headers(request.headers.items(), cfg.hidden_headers),
            "payload": limit_payload(
                _decode(_request_body(library, request), request.headers, hidden),
                cfg.max_body_kb,
            ),
            "status": None,
            "response_headers": {},
            "response_body": None,
            "redirects": 0,
            "duration_ms": round(duration, 2),
            "error": None,
        }
        if response is not None:
            content["status"] = response.status_code
            content["response_headers"] = redact_headers(
                response.headers.items(), cfg.hidden_headers
            )
            content["redirects"] = len(response.history)
            if content["redirects"]:
                content["final_url"] = redact_url(str(response.url), hidden)
            if not streamed and _is_text(response.headers.get("content-type", "")):
                body = _decode(response.content, response.headers, hidden)
                content["response_body"] = limit_payload(body, cfg.max_body_kb)
        if error is not None:
            content["error"] = {
                "class": f"{type(error).__module__}.{type(error).__qualname__}",
                "message": str(error),
            }
        tags = [f"host:{host}"] if host else []
        if error is not None:
            tags.append("failed")
        self.record(content, tags=tags)


def _request_body(library: str, request: Any) -> Any:
    if library == "requests":
        return request.body
    try:
        return request.content
    except Exception:  # noqa: BLE001 - httpx.RequestNotRead: a streamed body that wasn't read
        return _Streamed()


class _Streamed:
    """Marker for a body that can't be read without consuming it."""


def _mimetype(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def _is_text(content_type: str) -> bool:
    mimetype = _mimetype(content_type)
    return mimetype.endswith("json") or mimetype.startswith("text/")


def _decode(body: Any, headers: Any, hidden: Iterable[str]) -> Any:
    """A request or response body as JSON, form fields or text, with sensitive keys redacted."""
    if body is None or body == b"" or body == "":
        return None
    if isinstance(body, _Streamed) or not isinstance(body, (bytes, str)):
        return "[streamed body]"
    mimetype = _mimetype(headers.get("content-type", ""))
    if mimetype.startswith("multipart/"):
        return f"[multipart body: {len(body)} bytes]"
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return f"[binary body: {len(body)} bytes]"
    if mimetype.endswith("json"):
        try:
            return redact(json.loads(body), hidden)
        except ValueError:
            return body
    if mimetype == "application/x-www-form-urlencoded":
        return redact(parse_qs(body, keep_blank_values=True), hidden)
    return body
