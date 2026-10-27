"""Custom tags: dotted-path extraction, normalization and tagger callbacks."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .models import Entry

#: A tagger receives an entry and returns the tags to add (or ``None``).
Tagger = Callable[[Entry], Iterable[str] | None]

MAX_TAG_LENGTH = 255
MAX_VALUES_PER_PATH = 20


def resolve(data: Any, path: str) -> list[Any]:
    """Return every scalar found at the dotted ``path``.

    Lists are traversed transparently (``items.id`` yields the id of every item, ``items.0.id``
    only the first one) and JSON strings are parsed when the path continues through them,
    which lets a path reach into a stored response body.
    """
    found: list[Any] = []

    def walk(node: Any, parts: list[str]) -> None:
        if len(found) >= MAX_VALUES_PER_PATH:
            return
        if isinstance(node, str) and parts:
            try:
                node = json.loads(node)
            except ValueError:
                return
        if not parts:
            if isinstance(node, (list, tuple)):
                for item in node:
                    walk(item, [])
            elif node is not None and not isinstance(node, Mapping):
                found.append(node)
            return
        head, rest = parts[0], parts[1:]
        if isinstance(node, Mapping):
            if head in node:
                walk(node[head], rest)
        elif isinstance(node, (list, tuple)):
            if head.isdigit():
                if int(head) < len(node):
                    walk(node[int(head)], rest)
            else:
                for item in node:
                    walk(item, parts)

    walk(data, path.split("."))
    return found


def normalize(tags: Iterable[Any] | None) -> list[str]:
    """Strip, stringify, drop empties and duplicates (keeping order) and cap the length."""
    seen: dict[str, None] = {}
    for tag in tags or ():
        text = str(tag).strip()[:MAX_TAG_LENGTH]
        if text:
            seen[text] = None
    return list(seen)


def from_paths(content: Mapping[str, Any], spec: Mapping[str, str | Iterable[str]]) -> list[str]:
    """Build ``name:value`` tags from ``{"name": "dotted.path" | ["path", ...]}``."""
    tags = []
    for name, paths in spec.items():
        for path in [paths] if isinstance(paths, str) else paths:
            tags.extend(f"{name}:{value}" for value in resolve(content, path))
    return tags
