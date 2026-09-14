"""Glob-based file/directory exclusion for the watcher, scanner and recovery.

Patterns match path-component NAMES: ``_blobs`` skips every ``_blobs``
directory (and its subtree) at any depth, ``*.tmp`` every file with that
suffix. A pattern containing a path separator can never match and is
rejected by :func:`normalize_patterns`.

Two sources of patterns:

* the global list from ``Settings.ignore_globs()`` — every folder;
* a per-folder list stored on the folder's sync row (``ll_ignore``, JSON)
  — linked folders, whose owner knows which parts of a tree are noise.

:func:`for_folder` combines them and is what the scanner and the startup
recovery sweep use, so the two can never disagree about what is ignored.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable
from pathlib import PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..db.models import Folder


class IgnoreMatcher:
    """Match a path (or any of its ancestors) against a list of glob patterns."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns = tuple(patterns)

    def matches(self, rel_path: str | PurePath) -> bool:
        rel = PurePath(rel_path)
        for part in (rel, *rel.parents):
            name = part.name
            if not name:
                continue
            for pat in self._patterns:
                if fnmatch.fnmatch(name, pat):
                    return True
        return False


def from_settings() -> IgnoreMatcher:
    from ..config import get_settings

    return IgnoreMatcher(get_settings().ignore_globs())


def parse_patterns(raw: str | None) -> list[str]:
    """Decode a JSON list of patterns stored on a sync row. Tolerant: NULL,
    malformed JSON or a non-list decodes to ``[]``; non-string items are dropped."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str) and item.strip()]


def normalize_patterns(patterns: Iterable[str]) -> list[str]:
    """Strip, drop blanks, dedup preserving order.

    Raises ``ValueError`` for a pattern containing a path separator: the
    matcher tests path-component names, so ``a/b`` could never match and
    would be a silent no-op — better to tell the user at save time.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in patterns:
        pat = (raw or "").strip()
        if not pat:
            continue
        if "/" in pat or "\\" in pat:
            raise ValueError(
                f"ignore pattern {pat!r} contains a path separator; patterns match "
                "single path segments (e.g. 'node_modules' or '*.tmp')"
            )
        if pat in seen:
            continue
        seen.add(pat)
        out.append(pat)
    return out


def for_folder(session: Session, folder: Folder) -> IgnoreMatcher:
    """Global patterns plus the folder's own (its sync row's ``ll_ignore``)."""
    from ..config import get_settings
    from ..db.models import FolderSyncSource

    patterns = list(get_settings().ignore_globs())
    src = session.get(FolderSyncSource, folder.id) if folder.id is not None else None
    if src is not None and src.ll_ignore:
        patterns.extend(parse_patterns(src.ll_ignore))
    return IgnoreMatcher(patterns)
