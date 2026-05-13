"""Unit tests for the .voitta.meta sidecar loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voitta_rag_enterprise.services import meta_sidecar as ms


def _write_meta(file_path: Path, data: dict) -> None:
    ms.sidecar_path(file_path).write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# sidecar_path
# ---------------------------------------------------------------------------


def test_sidecar_path(tmp_path: Path) -> None:
    f = tmp_path / "report.md"
    assert ms.sidecar_path(f) == tmp_path / "report.md.voitta.meta"


# ---------------------------------------------------------------------------
# load — missing / malformed / non-object
# ---------------------------------------------------------------------------


def test_load_returns_none_when_no_sidecar(tmp_path: Path) -> None:
    assert ms.load(tmp_path / "report.md") is None


def test_load_returns_none_on_invalid_json(tmp_path: Path) -> None:
    f = tmp_path / "report.md"
    ms.sidecar_path(f).write_text("not json!!!")
    assert ms.load(f) is None


def test_load_returns_none_when_not_object(tmp_path: Path) -> None:
    f = tmp_path / "report.md"
    ms.sidecar_path(f).write_text(json.dumps([1, 2, 3]))
    assert ms.load(f) is None


# ---------------------------------------------------------------------------
# load — timestamp parsing
# ---------------------------------------------------------------------------


def test_load_parses_created_and_modified(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    _write_meta(f, {
        "created": "2026-05-08T11:20:00-07:00",
        "modified": "2026-05-13T07:30:00-07:00",
    })
    meta = ms.load(f)
    assert meta is not None
    assert meta.created_at_ns is not None
    assert meta.modified_at_ns is not None
    # modified > created
    assert meta.modified_at_ns > meta.created_at_ns


def test_load_timestamps_none_when_absent(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    _write_meta(f, {"owner": "Alice"})
    meta = ms.load(f)
    assert meta is not None
    assert meta.created_at_ns is None
    assert meta.modified_at_ns is None


def test_load_handles_naive_datetime_as_utc(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    _write_meta(f, {"created": "2026-01-01T00:00:00"})
    meta = ms.load(f)
    assert meta is not None
    assert meta.created_at_ns == 1767225600_000_000_000


def test_load_invalid_timestamp_yields_none(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    _write_meta(f, {"created": "not-a-date"})
    meta = ms.load(f)
    assert meta is not None
    assert meta.created_at_ns is None


# ---------------------------------------------------------------------------
# load — payload fields
# ---------------------------------------------------------------------------


def test_load_payload_fields_prefixed(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    _write_meta(f, {
        "owner": "Nyota Uhura",
        "owner_email": "nuhura@aurora-ev.com",
        "owner_role": "PM",
        "system": "Google Drive",
        "version": "1.0",
        "tags": ["phase-0", "frozen"],
        "shared_with": ["team@aurora-ev.com"],
        "file": "doc.md",
    })
    meta = ms.load(f)
    assert meta is not None
    pf = meta.payload_fields
    assert pf["meta_owner"] == "Nyota Uhura"
    assert pf["meta_owner_email"] == "nuhura@aurora-ev.com"
    assert pf["meta_owner_role"] == "PM"
    assert pf["meta_system"] == "Google Drive"
    assert pf["meta_version"] == "1.0"
    assert pf["meta_tags"] == ["phase-0", "frozen"]
    assert pf["meta_shared_with"] == ["team@aurora-ev.com"]
    assert pf["meta_file"] == "doc.md"


def test_load_ignores_unknown_fields(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    _write_meta(f, {"owner": "Alice", "size_bytes": 1234, "mime_type": "text/markdown"})
    meta = ms.load(f)
    assert meta is not None
    assert "meta_size_bytes" not in meta.payload_fields
    assert "meta_mime_type" not in meta.payload_fields
    assert meta.payload_fields["meta_owner"] == "Alice"


def test_load_partial_sidecar(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    _write_meta(f, {"tags": ["draft"]})
    meta = ms.load(f)
    assert meta is not None
    assert meta.payload_fields == {"meta_tags": ["draft"]}
    assert meta.created_at_ns is None
    assert meta.modified_at_ns is None
