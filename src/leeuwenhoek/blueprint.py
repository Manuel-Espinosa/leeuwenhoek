"""Panel views: Jinja + Bulma, rendered on the server."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import Markup, escape
from werkzeug.wrappers import Response as BaseResponse

from .auth import CSRF_COOKIE, check_access, csrf_token, verify_csrf
from .models import Entry, utcnow
from .storage.base import Page
from .watchers.query import KEYWORD_RE, n_plus_one_summary

bp = Blueprint(
    "leeuwenhoek",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

#: entry type -> URL slug
SLUGS = {
    "request": "requests",
    "query": "queries",
    "exception": "exceptions",
    "log": "logs",
    "dump": "dumps",
    "mail": "mail",
    "template": "templates",
}
TYPES = {slug: type_ for type_, slug in SLUGS.items()}
BATCHES = "batches"


def lw():
    return current_app.extensions["leeuwenhoek"]


# ---------------------------------------------------------------------- access


@bp.before_request
def _guard() -> Response | None:
    response = check_access(lw())
    if response is None and request.method == "POST":
        verify_csrf()
    return response


@bp.after_request
def _set_csrf_cookie(response: Response) -> Response:
    token = request.environ.get("leeuwenhoek.csrf")
    if token:
        response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="Strict")
    return response


# ------------------------------------------------------------------- context


def sections() -> list[dict[str, Any]]:
    counts = lw().storage.counts()
    items = []
    for watcher in lw().watchers.values():
        slug = SLUGS.get(watcher.entry_type)
        if slug is None:
            continue
        items.append(
            {
                "slug": slug,
                "key": watcher.entry_type,  # key in storage.counts() and /api/status
                "label": watcher.label,
                "count": counts.get(watcher.entry_type, 0),
            }
        )
    # Requests first, then Batches, then the rest in watcher order.
    items.sort(key=lambda s: s["slug"] != "requests")
    items.insert(
        min(1, len(items)),
        {"slug": BATCHES, "key": "batches", "label": "Batches", "count": counts.get("batches", 0)},
    )
    return items


def _poll_seconds() -> float:
    """Live polling interval; 0 (or less) disables polling."""
    return max(0, lw().config.poll_seconds or 0)


@bp.context_processor
def _inject() -> dict[str, Any]:
    return {
        "lw": lw(),
        "sections": sections(),
        "recording": lw().is_recording(),
        "csrf_token": csrf_token,
        "slug_for": lambda type_: SLUGS.get(type_, type_),
        "use_cdn": lw().config.bulma_cdn,
        "poll_seconds": _poll_seconds(),
    }


# ---------------------------------------------------------------------- filters


@bp.app_template_filter("lw_duration")
def lw_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    return f"{ms:.0f} ms" if ms >= 10 else f"{ms:.2f} ms"


@bp.app_template_filter("lw_kb")
def lw_kb(kb: float | None) -> str:
    if kb is None:
        return "—"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"


@bp.app_template_filter("lw_timeago")
def lw_timeago(when: datetime | None) -> str:
    if when is None:
        return "—"
    seconds = int((utcnow() - when).total_seconds())
    if seconds < 5:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return f"{seconds} s ago"


@bp.app_template_filter("lw_truncate")
def lw_truncate(text: Any, length: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= length else text[: length - 1] + "…"


@bp.app_template_filter("lw_json")
def lw_json(value: Any) -> str:
    return json.dumps(value, indent=2, default=repr, ensure_ascii=False)


_CLAUSE_RE = re.compile(
    r"\s+(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|LEFT JOIN|INNER JOIN|JOIN)\b", re.IGNORECASE
)


@bp.app_template_filter("lw_sql")
def lw_sql(sql: str | None) -> Markup:
    """Escaped SQL, with a line break before each clause and keywords highlighted."""
    if not sql:
        return Markup("")
    text = _CLAUSE_RE.sub(lambda m: "\n" + m.group(1), sql.strip())
    html = str(escape(text))
    return Markup(KEYWORD_RE.sub(r'<span class="lw-kw">\1</span>', html))


# ---------------------------------------------------------------------- views


@bp.get("/")
def home() -> BaseResponse:
    return redirect(url_for("leeuwenhoek.listing", slug=sections()[0]["slug"]))


def _links(slug: str, page: Page, args: Mapping[str, Any]) -> tuple[str | None, str | None]:
    older = (
        url_for("leeuwenhoek.listing", slug=slug, before=page.older, **args) if page.older else None
    )
    newer = (
        url_for("leeuwenhoek.listing", slug=slug, after=page.newer, **args) if page.newer else None
    )
    return older, newer


def _listing_args(slug: str) -> dict[str, Any]:
    """404 for an inactive section; otherwise the filters (``tag``, ``q``) of the request."""
    if slug not in {s["slug"] for s in sections()}:
        abort(404)
    return {k: v for k in ("tag", "q") if (v := request.args.get(k))}


@bp.get("/<slug>")
def listing(slug: str) -> str:
    args = _listing_args(slug)
    before = request.args.get("before", type=int)
    page = lw().storage.page(
        None if slug == BATCHES else TYPES[slug],
        root_only=slug == BATCHES,
        before=before,
        after=request.args.get("after", type=int),
        limit=lw().config.page_size,
        **args,
    )
    older, newer = _links(slug, page, args)
    # Poll for new rows only on the newest page: an older one must stay put while it is read.
    rows_url = None
    if _poll_seconds() and before is None and newer is None:
        rows_url = url_for("leeuwenhoek.rows", slug=slug, **args)
    return render_template(
        "leeuwenhoek/index.html",
        slug=slug,
        entry_type=None if slug == BATCHES else TYPES[slug],
        title=next(s["label"] for s in sections() if s["slug"] == slug),
        entries=page.entries,
        older_url=older,
        newer_url=newer,
        tag=args.get("tag", ""),
        q=args.get("q", ""),
        rows_url=rows_url,
    )


@bp.get("/<slug>/rows")
def rows(slug: str) -> str:
    """The ``<tr>`` rows of the entries newer than ``after``, for live polling.

    Same filters as the listing. At most ``PAGE_SIZE`` rows, the oldest first after the cursor
    (newest on top), so a client that falls behind catches up over a few polls.
    """
    args = _listing_args(slug)
    entries = lw().storage.list(
        None if slug == BATCHES else TYPES[slug],
        root_only=slug == BATCHES,
        after=request.args.get("after", type=int),
        limit=lw().config.page_size,
        **args,
    )
    return render_template(
        "leeuwenhoek/_rows.html",
        entries=entries,
        entry_type=None if slug == BATCHES else TYPES[slug],
    )


@bp.get("/<slug>/<uuid>")
def detail(slug: str, uuid: str) -> str:
    entry = lw().storage.get(uuid)
    if entry is None or SLUGS.get(entry.type) != slug:
        abort(404)
    return render_template(
        "leeuwenhoek/detail.html",
        entry=entry,
        related=_group(lw().storage.batch(entry.batch_id), skip=entry),
    )


@bp.get("/batch/<batch_id>")
def batch(batch_id: str) -> str:
    entries = lw().storage.batch(batch_id)
    if not entries:
        abort(404)
    main = next((e for e in entries if e.type == "request"), None)
    return render_template(
        "leeuwenhoek/batch.html",
        batch_id=batch_id,
        main=main,
        groups=_group(entries, skip=main),
        total=len(entries),
    )


@bp.get("/exceptions/family/<hash>")
def family(hash: str) -> str:
    entries = lw().storage.family(hash)
    if not entries:
        abort(404)
    return render_template("leeuwenhoek/family.html", entries=entries, first=entries[0])


def _group(entries: list[Entry], skip: Entry | None = None) -> list[dict[str, Any]]:
    """Group by type and compute each group's count, total time and slow entries.

    The query group also gets ``n_plus_one``: the repeated statements of the whole batch (the
    skipped entry included) with their counts.
    """
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        if skip is not None and entry.uuid == skip.uuid:
            continue
        groups.setdefault(entry.type, []).append(entry)
    order = list(SLUGS)
    return [
        {
            "type": type_,
            "label": type_.capitalize() + ("ies" if type_ == "query" else "s"),
            "entries": items,
            "count": len(items),
            "total_ms": sum(i.content.get("duration_ms") or 0 for i in items) or None,
            "slow": sum(1 for i in items if i.content.get("slow")),
            "n_plus_one": n_plus_one_summary(entries) if type_ == "query" else [],
        }
        for type_, items in sorted(groups.items(), key=lambda kv: order.index(kv[0]))
    ]


@bp.post("/recording/toggle")
def toggle_recording() -> BaseResponse:
    lw().set_recording(not lw().is_recording())
    return redirect(request.referrer or url_for("leeuwenhoek.home"))


@bp.post("/clear")
def clear() -> BaseResponse:
    lw().flush()
    lw().storage.clear()
    return redirect(url_for("leeuwenhoek.home"))


@bp.get("/api/status")
def status() -> Response:
    return jsonify(recording=lw().is_recording(), counts=lw().storage.counts())
