"""services/source_meta.py — capture normalization + payload expansion."""

from __future__ import annotations

from voitta_rag_enterprise.services import source_meta as sm


def test_iso_to_epoch_handles_z_and_offset_and_garbage() -> None:
    assert sm.iso_to_epoch("2024-01-02T03:04:05Z") == 1704164645
    assert sm.iso_to_epoch("2024-01-02T03:04:05+00:00") == 1704164645
    assert sm.iso_to_epoch("not-a-date") is None
    assert sm.iso_to_epoch(None) is None
    assert sm.iso_to_epoch("") is None


def test_build_drops_blanks_and_normalizes_dates() -> None:
    d = sm.build(
        owner_name="Roman", owner_email="r@x.com",
        editor_name="", editor_email=None,          # dropped
        shared_by_email="grp@x.com",
        created="2024-01-02T03:04:05Z",             # → epoch
        modified=1700000000,                        # int passthrough
    )
    assert d == {
        "owner_name": "Roman",
        "owner_email": "r@x.com",
        "shared_by_email": "grp@x.com",
        "created_ts": 1704164645,
        "modified_ts": 1700000000,
    }
    assert sm.build() == {}  # nothing known → empty


def test_payload_fields_prefixes_omits_nulls_and_adds_uploaded() -> None:
    pf = sm.payload_fields(
        {"owner_email": "r@x.com", "created_ts": 100, "modified_ts": 200},
        uploaded_ts=300,
    )
    assert pf == {
        "meta_owner_email": "r@x.com",
        "meta_created_ts": 100,
        "meta_modified_ts": 200,
        "meta_uploaded_ts": 300,
    }
    # No keys for absent fields (compact, exact-filter-safe).
    assert "meta_owner_name" not in pf


def test_payload_fields_modified_fallback_only_when_source_missing() -> None:
    # source has modified → fallback ignored
    a = sm.payload_fields({"modified_ts": 200}, modified_fallback_ts=999)
    assert a["meta_modified_ts"] == 200
    # source missing modified → fallback used
    b = sm.payload_fields(None, uploaded_ts=300, modified_fallback_ts=999)
    assert b == {"meta_modified_ts": 999, "meta_uploaded_ts": 300}


def test_payload_fields_empty_when_nothing() -> None:
    assert sm.payload_fields(None) == {}
    assert sm.payload_fields({}) == {}
