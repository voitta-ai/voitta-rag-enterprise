"""Glob-based file/directory exclusion for the watcher and scanner."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import PurePath


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
