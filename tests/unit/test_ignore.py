"""Tests for the IgnoreMatcher."""

from __future__ import annotations

from voitta_image_rag.services.ignore import IgnoreMatcher


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
