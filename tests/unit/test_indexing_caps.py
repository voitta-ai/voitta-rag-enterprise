"""Tests for the admin-managed indexing caps store.

Covers: defaults, override-file round trip, clamping to BOUNDS,
unknown-key drop, non-int rejection, cache invalidation, and the
Settings-derived env overlay for fields that have both representations.
"""

from __future__ import annotations

import json


def test_defaults_match_dataclass(env: None) -> None:
    from voitta_rag_enterprise.services import indexing_caps

    d = indexing_caps.defaults_dict()
    # Pin a couple of canonical defaults so the dataclass can't be
    # silently re-tuned without a test failure.
    assert d["max_file_bytes"] == 1024 * 1024 * 1024
    assert d["data_file_max_bytes"] == 5 * 1024 * 1024
    assert d["xlsx_max_rows"] == 50_000
    assert d["pdf_pages_per_bucket"] == 20


def test_get_caps_with_no_override_returns_defaults(env: None) -> None:
    from voitta_rag_enterprise.services import indexing_caps

    indexing_caps.invalidate_cache()
    caps = indexing_caps.get_caps()
    assert caps.xlsx_max_rows == 50_000
    assert caps.ipynb_max_output_chars == 2_000


def test_update_persists_and_round_trips(env: None) -> None:
    from voitta_rag_enterprise.services import indexing_caps

    out = indexing_caps.update({"xlsx_max_rows": 1000, "xlsx_max_cols": 8})
    assert out.xlsx_max_rows == 1000
    assert out.xlsx_max_cols == 8

    # The file on disk only carries the override, not the full snapshot.
    p = indexing_caps._path()
    raw = json.loads(p.read_text())
    assert raw == {"xlsx_max_rows": 1000, "xlsx_max_cols": 8}

    # Re-loading after a cache wipe reflects the persisted override.
    indexing_caps.invalidate_cache()
    caps = indexing_caps.get_caps()
    assert caps.xlsx_max_rows == 1000
    assert caps.xlsx_max_cols == 8
    # And untouched fields keep their defaults.
    assert caps.pdf_pages_per_bucket == 20


def test_update_clamps_to_bounds(env: None) -> None:
    from voitta_rag_enterprise.services import indexing_caps

    # 10 is below the floor for pdf_parse_timeout_s? The bound is (10, 7200),
    # so the floor is exactly 10 — push below it.
    out = indexing_caps.update({"pdf_parse_timeout_s": 1})
    assert out.pdf_parse_timeout_s == 10
    # And above the ceiling.
    out = indexing_caps.update({"pdf_parse_timeout_s": 999_999})
    assert out.pdf_parse_timeout_s == 7200


def test_update_drops_unknown_keys(env: None) -> None:
    from voitta_rag_enterprise.services import indexing_caps

    out = indexing_caps.update({"xlsx_max_rows": 100, "totally_unknown": 42})
    assert out.xlsx_max_rows == 100
    raw = json.loads(indexing_caps._path().read_text())
    assert "totally_unknown" not in raw


def test_update_rejects_non_integer(env: None) -> None:
    from voitta_rag_enterprise.services import indexing_caps
    import pytest as _pytest

    with _pytest.raises(ValueError):
        indexing_caps.update({"xlsx_max_rows": "lots"})
    with _pytest.raises(ValueError):
        indexing_caps.update({"xlsx_max_rows": True})  # bool is int subclass; explicitly excluded


def test_invalidate_cache_picks_up_disk_edit(env: None) -> None:
    """Recovery path: an admin SSHing in and hand-editing the JSON file
    should be reflected after the next cache invalidation."""
    from voitta_rag_enterprise.services import indexing_caps

    # Seed via the API so the file exists in the right shape.
    indexing_caps.update({"xlsx_max_rows": 1000})
    indexing_caps._path().write_text(json.dumps({"xlsx_max_rows": 9999}))
    indexing_caps.invalidate_cache()
    assert indexing_caps.get_caps().xlsx_max_rows == 9999


def test_env_overlay_provides_settings_defaults(env: None, monkeypatch) -> None:
    """A field that has a Settings equivalent picks up its env-derived
    default until an override is written."""
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.services import indexing_caps

    monkeypatch.setenv("VOITTA_PDF_PAGES_PER_BUCKET", "33")
    reset_settings_cache()
    indexing_caps.invalidate_cache()
    assert indexing_caps.get_caps().pdf_pages_per_bucket == 33
