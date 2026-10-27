"""Panel access control and own CSRF token."""

from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING

from flask import Response, abort, request

if TYPE_CHECKING:
    from . import Leeuwenhoek

CSRF_COOKIE = "lw_csrf"
CSRF_FIELD = "csrf_token"


def check_access(lw: Leeuwenhoek) -> Response | None:
    """``None`` if access is allowed; otherwise 404 (disabled), 401 (basic auth) or 403."""
    if not lw.enabled():
        abort(404)
    if lw._authorize is not None:
        if not lw._authorize():
            abort(403)
    elif not lw.app.debug and lw.config.basic_auth is None:
        abort(403)
    credentials = lw.config.basic_auth
    if credentials is not None:
        given = request.authorization
        user, password = credentials
        ok = (
            given is not None
            and hmac.compare_digest(given.username or "", user)
            and hmac.compare_digest(given.password or "", password)
        )
        if not ok:
            return Response(
                "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Leeuwenhoek"'}
            )
    return None


def csrf_token() -> str:
    """Double-submit cookie token; works even if the app has no ``SECRET_KEY``."""
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = request.environ.setdefault("leeuwenhoek.csrf", secrets.token_urlsafe(24))
    return token


def verify_csrf() -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = request.form.get(CSRF_FIELD, "")
    if not cookie or not hmac.compare_digest(cookie, sent):
        abort(400, "Invalid CSRF token")
