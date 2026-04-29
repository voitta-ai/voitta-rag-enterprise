"""Tests for the Settings loader."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_defaults_apply(env: None) -> None:
    from voitta_image_rag.config import get_settings

    s = get_settings()
    assert s.port == 8000
    assert s.mcp_port == 8001
    assert s.dense_model == "intfloat/e5-base-v2"
    assert s.sparse_model == "Qdrant/bm25"
    assert s.nearby_radius == 2
    assert s.single_user is False
    assert s.dev_user is None
    assert s.max_file_bytes == 1024 * 1024 * 1024


def test_env_overrides(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_image_rag.config import get_settings, reset_settings_cache

    monkeypatch.setenv("VOITTA_PORT", "9000")
    monkeypatch.setenv("VOITTA_NEARBY_RADIUS", "5")
    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    reset_settings_cache()

    s = get_settings()
    assert s.port == 9000
    assert s.nearby_radius == 5
    assert s.single_user is True
    assert s.dev_user == "alice@example.com"


def test_path_resolution_defaults(env: None) -> None:
    from voitta_image_rag.config import get_settings

    s = get_settings()
    assert s.resolved_db_path() == s.data_dir / "voitta.db"
    assert s.resolved_cas_dir() == s.data_dir / "cas"
    assert s.resolved_qdrant_path() == s.data_dir / "qdrant"


def test_db_path_override(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from voitta_image_rag.config import get_settings, reset_settings_cache

    custom = tmp_path / "elsewhere.db"
    monkeypatch.setenv("VOITTA_DB_PATH", str(custom))
    reset_settings_cache()

    assert get_settings().resolved_db_path() == custom


def test_data_dir_expands_tilde(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_image_rag.config import get_settings, reset_settings_cache

    monkeypatch.setenv("VOITTA_DATA_DIR", "~/some-place")
    reset_settings_cache()

    s = get_settings()
    assert "~" not in str(s.data_dir)
    assert s.data_dir.is_absolute()


def test_ignore_globs_parsing(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_image_rag.config import get_settings, reset_settings_cache

    monkeypatch.setenv("VOITTA_IGNORE_PATTERNS", " a , b ,, c ")
    reset_settings_cache()

    assert get_settings().ignore_globs() == ["a", "b", "c"]


def test_workers_defaults_to_cpu_count(env: None) -> None:
    from voitta_image_rag.config import get_settings

    assert get_settings().resolved_workers() >= 1


def test_workers_explicit_override(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_image_rag.config import get_settings, reset_settings_cache

    monkeypatch.setenv("VOITTA_WORKERS", "7")
    reset_settings_cache()

    assert get_settings().resolved_workers() == 7
