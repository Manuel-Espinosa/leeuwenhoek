"""Requests: method, URL, headers, body, session, user, response and duration."""

from __future__ import annotations

import time
from importlib.util import find_spec
from typing import Any

from flask import Flask, Response, request, session

from ..context import Batch
from ..filters import limit_payload, redact, redact_headers
from ..tags import from_paths
from .base import Watcher

START = "leeuwenhoek.started"


class RequestWatcher(Watcher):
    name = "request"
    entry_type = "request"
    label = "Requests"

    def register(self, app: Flask) -> None:
        app.before_request(self._start)
        app.after_request(self._capture_response)
        self.lw.on_finish.append(self._finish)

    def _start(self) -> None:
        request.environ[START] = time.perf_counter()

    def _capture_response(self, response: Response) -> Response:
        batch = self.lw.current()
        if batch is None or not batch.recording:
            return response
        cfg = self.lw.config
        meta: dict[str, Any] = {
            "status": response.status_code,
            "response_headers": redact_headers(response.headers, cfg.hidden_headers),
        }
        if not response.direct_passthrough and not response.is_streamed:
            mimetype = response.mimetype or ""
            if mimetype.endswith("json") or mimetype.startswith("text/"):
                body = response.get_data(as_text=True)
                meta["response_body"] = limit_payload(body, cfg.max_body_kb)
        batch.meta.update(meta)
        return response

    def _finish(self, batch: Batch, exc: BaseException | None) -> None:
        if batch.kind != "request":
            return
        cfg = self.lw.config
        hidden = cfg.hidden_params
        started = request.environ.get(START)
        meta = batch.meta
        status = meta.get("status", 500 if exc else None)
        user_id = self._user_id()
        rule = request.url_rule

        content = {
            "method": request.method,
            "url": request.url,
            "path": request.path,
            "rule": rule.rule if rule else None,
            "endpoint": request.endpoint,
            "blueprint": request.blueprint,
            "view_args": request.view_args or {},
            "ip": request.remote_addr,
            "headers": redact_headers(request.headers, cfg.hidden_headers),
            "query": redact(request.args.to_dict(flat=False), hidden),
            "body": limit_payload(redact(self._body(), hidden), cfg.max_body_kb),
            "session": redact(dict(session), hidden) if request.cookies else {},
            "user": user_id,
            "status": status,
            "response_headers": meta.get("response_headers", {}),
            "response_body": meta.get("response_body"),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2) if started else None,
        }
        tags = []
        if request.endpoint:
            tags.append(f"endpoint:{request.endpoint}")
        if status is not None:
            tags.append(f"status:{status}")
        if user_id is not None:
            tags.append(f"user:{user_id}")
        batch.tags.extend(from_paths(content, cfg.request_tags))
        self.record(content, tags=tags, is_root=True, force=True)

    @staticmethod
    def _body() -> Any:
        if request.is_json:
            data = request.get_json(silent=True)
            if data is not None:
                return data
        if request.form:
            return request.form.to_dict(flat=False)
        data = request.get_data(cache=True, as_text=True)
        return data or None

    @staticmethod
    def _user_id() -> str | None:
        if find_spec("flask_login") is None:
            return None
        try:
            from flask_login import current_user

            if current_user and current_user.is_authenticated:
                return str(current_user.get_id())
        except Exception:  # noqa: BLE001 - the app may have no LoginManager
            return None
        return None
