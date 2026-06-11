"""Tests for the Google Drive local-sync connector (desktop, no credentials).

The load-bearing guarantee is **read-only**: we never write inside
``~/Library/CloudStorage``. These tests pin the write-safety helpers, stub
detection, path-safety, native-doc parsing, and the connector's sidecar
location. Tests that need a real mounted Google Drive are skipped when one
isn't present, so the suite still runs on Linux/CI.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from voitta_rag_enterprise.services.sync import cloudstorage_local as cl

_LIVE_ACCOUNTS = cl.list_accounts() if cl.is_macos() else []
_needs_drive = pytest.mark.skipif(
    not _LIVE_ACCOUNTS, reason="no mounted Google Drive on this machine"
)


# ---------------------------------------------------------------------------
# Write-safety — the "never touch the Drive" guard
# ---------------------------------------------------------------------------


def test_is_within_cloud_storage_true_for_mount():
    p = cl.CLOUD_STORAGE_ROOT / "GoogleDrive-foo@bar.com" / "My Drive" / "x.pdf"
    assert cl.is_within_cloud_storage(p)


def test_is_within_cloud_storage_false_for_outside():
    assert not cl.is_within_cloud_storage("/tmp/x.txt")
    assert not cl.is_within_cloud_storage("/etc/passwd")
    assert not cl.is_within_cloud_storage(os.path.expanduser("~/Documents/x"))


def test_assert_read_only_path_refuses_writes_under_cloud_storage():
    target = cl.CLOUD_STORAGE_ROOT / "GoogleDrive-x" / "note.txt"
    with pytest.raises(PermissionError):
        cl.assert_read_only_path(target)


def test_assert_read_only_path_allows_writes_outside():
    cl.assert_read_only_path("/tmp/anything.json")  # must not raise


# ---------------------------------------------------------------------------
# Stub detection + native-doc classification (no real Drive needed)
# ---------------------------------------------------------------------------


def test_is_native_doc():
    assert cl.is_native_doc("Plan.gdoc")
    assert cl.is_native_doc("Budget.gsheet")
    assert cl.is_native_doc("Deck.gslides")
    assert not cl.is_native_doc("report.pdf")
    assert not cl.is_native_doc("data.xlsx")


def test_is_stub_on_regular_file(tmp_path):
    f = tmp_path / "real.txt"
    f.write_text("hello world" * 100)
    assert not cl.is_stub(f)  # real file has allocated blocks


def test_read_gdoc_pointer(tmp_path):
    g = tmp_path / "My Doc.gdoc"
    g.write_text(json.dumps({"doc_id": "ABC123", "email": "u@x.com"}))
    ptr = cl.read_gdoc_pointer(g)
    assert ptr is not None
    assert ptr.doc_id == "ABC123"
    assert ptr.kind == "document"
    assert ptr.export_format == "pdf"
    assert ptr.url and "ABC123" in ptr.url


def test_read_gdoc_pointer_gsheet_exports_xlsx(tmp_path):
    g = tmp_path / "Sheet.gsheet"
    g.write_text(json.dumps({"doc_id": "S1"}))
    ptr = cl.read_gdoc_pointer(g)
    assert ptr is not None and ptr.kind == "spreadsheets" and ptr.export_format == "xlsx"


def test_read_gdoc_pointer_rejects_garbage(tmp_path):
    g = tmp_path / "bad.gdoc"
    g.write_text("not json")
    assert cl.read_gdoc_pointer(g) is None


# ---------------------------------------------------------------------------
# browse path-safety (no real Drive needed)
# ---------------------------------------------------------------------------


def test_browse_rejects_paths_outside_cloud_storage():
    with pytest.raises(ValueError):
        cl.browse("/etc")
    with pytest.raises(ValueError):
        cl.browse(os.path.expanduser("~"))


# ---------------------------------------------------------------------------
# Connector sidecar location — must live OUTSIDE the Drive mount
# ---------------------------------------------------------------------------


def test_cloud_sidecar_path_is_outside_drive():
    from voitta_rag_enterprise.services.sync.cloud_local import cloud_sidecar_path

    p = cloud_sidecar_path(123)
    assert not cl.is_within_cloud_storage(p)
    assert p.name == "123.json"


def test_connector_registered():
    from voitta_rag_enterprise.services.sync.registry import get_registry

    reg = get_registry()
    assert "google_drive_local" in reg.list_types()
    conn = reg.get("google_drive_local")
    assert conn.source_type == "google_drive_local"
    assert conn.supports_progress is True


def test_sync_stats_object_matches_run_sync_contract():
    """run_sync treats the connector's return value as a stats OBJECT —
    ``stats.as_dict()`` and ``stats.errors`` — not a dict. Guards against the
    regression where sync() returned ``stats.as_dict()`` and run_sync then hit
    ``'dict' object has no attribute 'as_dict'``."""
    from voitta_rag_enterprise.services.sync.cloud_local import CloudLocalSyncStats

    stats = CloudLocalSyncStats(files_seen=3, stubs=3)
    # The two attributes run_sync depends on must exist on the OBJECT.
    assert hasattr(stats, "errors")
    assert isinstance(stats.errors, list)
    d = stats.as_dict()
    assert isinstance(d, dict) and d["files_seen"] == 3


# ---------------------------------------------------------------------------
# Live-Drive tests (skipped without a mounted Google Drive)
# ---------------------------------------------------------------------------


@_needs_drive
def test_live_accounts_have_email_and_path():
    for a in _LIVE_ACCOUNTS:
        assert "@" in a.email
        assert cl.is_within_cloud_storage(a.path)
        assert a.provider == "google_drive"


@_needs_drive
def test_live_browse_is_free_and_safe():
    acct = _LIVE_ACCOUNTS[0]
    entries = cl.browse(os.path.join(acct.path, "My Drive"))
    # Every returned path stays within CloudStorage.
    for e in entries:
        assert cl.is_within_cloud_storage(e.path)


def test_sync_source_out_exposes_saved_paths():
    """The dialog pre-checks the saved subtrees on reopen — the API must
    return them (JSON list first, legacy single path as fallback)."""
    import json

    from voitta_rag_enterprise.api.routes.sync import _to_out
    from voitta_rag_enterprise.db.models import FolderSyncSource

    paths = ["/mount/Shared drives/X/NDAs", "/mount/Shared drives/X/Ops"]
    src = FolderSyncSource(
        folder_id=1,
        source_type="google_drive_local",
        sync_status="idle",
        auto_sync_enabled=False,
        auto_sync_hours=6,
        gdl_account="me@example.com",
        gdl_path=paths[0],
        gdl_paths=json.dumps(paths),
    )
    out = _to_out(src)
    assert out.google_drive_local is not None
    assert out.google_drive_local.paths == paths

    src.gdl_paths = None  # legacy row: single path only
    assert _to_out(src).google_drive_local.paths == [paths[0]]
