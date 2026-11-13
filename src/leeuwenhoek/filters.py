"""Ignored paths, entry filters, redaction and truncation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from fnmatch import fnmatch
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Entry, dumps

#: An entry filter receives an entry and returns ``True`` to keep it.
EntryFilter = Callable[[Entry], bool]

MASK = "********"


def is_ignored(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def redact_headers(headers: Iterable[tuple[str, str]], hidden: Iterable[str]) -> dict[str, str]:
    hidden_lower = {h.lower() for h in hidden}
    return {k: (MASK if k.lower() in hidden_lower else v) for k, v in headers}


def redact(value: Any, hidden: Iterable[str]) -> Any:
    """Replace values of sensitive keys with ``********``, at any depth."""
    hidden_lower = {h.lower() for h in hidden}

    def walk(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {k: MASK if str(k).lower() in hidden_lower else walk(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(v) for v in item]
        return item

    return walk(value)


def redact_url(url: str, hidden: Iterable[str]) -> str:
    """Mask sensitive query parameters and the userinfo password of a URL."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if parts.password is not None:
        userinfo, _, host = netloc.rpartition("@")
        netloc = f"{userinfo.partition(':')[0]}:{MASK}@{host}"
    query = parts.query
    if query:
        hidden_lower = {h.lower() for h in hidden}
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(k.lower() in hidden_lower for k, _ in pairs):
            query = urlencode(
                [(k, MASK if k.lower() in hidden_lower else v) for k, v in pairs], safe="*"
            )
    return urlunsplit(parts._replace(netloc=netloc, query=query))


def limit_payload(value: Any, max_kb: int) -> Any:
    """Return ``value`` as is if it fits; otherwise a truncated string with a marker."""
    limit = max_kb * 1024
    text = value if isinstance(value, str) else dumps(value)
    if len(text) <= limit:
        return value
    return text[:limit] + f"… [truncated: {len(text)} characters]"
