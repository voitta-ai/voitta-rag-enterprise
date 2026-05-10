"""Tests for the IgnoreMatcher."""

from __future__ import annotations

from voitta_rag_enterprise.services.ignore import IgnoreMatcher


def test_matches_filename_glob() -> None:
    m = IgnoreMatcher(["*.tmp", ".DS_Store"])
    assert m.matches("foo.tmp")
    assert m.matches("a/b/.DS_Store")
    assert not m.matches("foo.txt")


def test_matches_directory_anywhere_in_path() -> None:
    m = IgnoreMatcher(["node_modules", ".git"])
    assert m.matches("node_modules/lodash/index.js")
    assert m.matches("a/.git/HEAD")
    assert not m.matches("modules/x.js")


def test_glob_pattern_in_subpath() -> None:
    m = IgnoreMatcher(["__pycache__", "*.pyc"])
    assert m.matches("a/__pycache__/x.pyc")
    assert m.matches("a/b/foo.pyc")
    assert not m.matches("a/b/foo.py")


def test_empty_patterns_matches_nothing() -> None:
    m = IgnoreMatcher([])
    assert not m.matches("anything")
    assert not m.matches("a/b/c")


def test_default_settings_ignore_voitta_sidecars() -> None:
    """The defaults must hide every sidecar a sync connector writes.

    Regression test for ``.voitta_sources.json`` showing up in indexed
    Google Drive folders — the GD connector writes both sidecars after
    every sync, and only ``.voitta_timestamps.json`` was on the ignore
    list, so the watcher fed the other one to the parser registry.
    """
    from voitta_rag_enterprise.config import get_settings, reset_settings_cache

    reset_settings_cache()
    try:
        m = IgnoreMatcher(get_settings().ignore_globs())
    finally:
        reset_settings_cache()
    for name in (
        ".voitta_sources.json",
        ".voitta_timestamps.json",
        ".voitta_sync.lock",
    ):
        assert m.matches(name), f"{name} should be ignored by default"
        assert m.matches(f"sub/dir/{name}"), f"{name} should be ignored even nested"

    # The full-workbook sidecar dir must be excluded from indexing — it
    # holds .xlsx files that pair with per-sheet markdown summaries; the
    # xlsx itself is fetched via the MCP voitta_rag_get_workbook tool.
    assert m.matches(".voitta_workbooks"), ".voitta_workbooks dir must be ignored"
    assert m.matches(".voitta_workbooks/Q4.xlsx"), ".voitta_workbooks/* must be ignored"
