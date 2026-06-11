"""gdl_connect (POST /api/sync/local/connect) — live-update contract.

The SPA caches each folder's sync config and trusts that cache when the
sync dialog opens; ``folder.sync_config_changed`` is the ONLY thing that
keeps it fresh. gdl_connect historically skipped that publish, so after
connecting Google Drive the dialog reopened to an empty GitHub form until
a full page reload. These tests pin the contract: connect emits the full
config event plus a field-complete ``folder.sync_source_changed``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from voitta_rag_enterprise.services import events
from voitta_rag_enterprise.services.sync import cloudstorage_local


def _gdl_env(monkeypatch, mount: Path) -> None:
    """Bypass the macOS / CloudStorage / Drive-app gates; keep is_dir real."""
    monkeypatch.setattr(cloudstorage_local, "is_macos", lambda: True)
    monkeypatch.setattr(cloudstorage_local, "is_within_cloud_storage", lambda p: True)
    monkeypatch.setattr(cloudstorage_local, "is_source_live", lambda p: True)


def test_gdl_connect_publishes_full_config(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    src_dir = tmp_path / "folder"
    src_dir.mkdir()
    fid = client.post("/api/folders", json={"name": src_dir.name}).json()["id"]

    mount = tmp_path / "GoogleDrive-me@example.com"
    subtree = mount / "Shared drives" / "X" / "NDAs"
    subtree.mkdir(parents=True)
    _gdl_env(monkeypatch, mount)

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        events, "publish", lambda topic, ev: published.append((topic, ev))
    )

    r = client.post(
        "/api/sync/local/connect",
        json={
            "folder_id": fid,
            "account": "me@example.com",
            "account_root": str(mount),
            "paths": [str(subtree)],
            "auto_sync_enabled": False,
            "auto_sync_hours": 6,
        },
    )
    assert r.status_code == 200, r.text

    by_type = {ev["type"]: ev for _, ev in published}

    # The config event the dialog's cache lives on — full SyncSourceOut shape.
    cfg_ev = by_type["folder.sync_config_changed"]
    assert cfg_ev["folder_id"] == fid
    cfg = cfg_ev["config"]
    assert cfg["source_type"] == "google_drive_local"
    assert cfg["google_drive_local"]["paths"] == [str(subtree)]
    assert cfg["google_drive_local"]["account"] == "me@example.com"

    # The status event must be field-complete (ws.js stores these keys).
    src_ev = by_type["folder.sync_source_changed"]
    assert src_ev["sync_status"] == "idle"
    assert "sync_error" in src_ev and "last_synced_at" in src_ev

    # And the dialog's REST path agrees with what was broadcast.
    got = client.get(f"/api/folders/{fid}/sync").json()
    assert got["source_type"] == "google_drive_local"
    assert got["google_drive_local"]["paths"] == [str(subtree)]


def test_gdl_connect_persists_canonical_paths(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """A child selection covered by an ancestor selection is dropped."""
    src_dir = tmp_path / "folder2"
    src_dir.mkdir()
    fid = client.post("/api/folders", json={"name": src_dir.name}).json()["id"]

    mount = tmp_path / "GoogleDrive-me@example.com"
    parent = mount / "Shared drives" / "X"
    child = parent / "NDAs"
    child.mkdir(parents=True)
    _gdl_env(monkeypatch, mount)

    r = client.post(
        "/api/sync/local/connect",
        json={
            "folder_id": fid,
            "account": "me@example.com",
            "account_root": str(mount),
            "paths": [str(parent), str(child)],
            "auto_sync_enabled": False,
            "auto_sync_hours": 6,
        },
    )
    assert r.status_code == 200, r.text

    from voitta_rag_enterprise.db.database import init_db, session_scope
    from voitta_rag_enterprise.db.models import FolderSyncSource

    init_db()
    with session_scope() as s:
        row = s.get(FolderSyncSource, fid)
        assert json.loads(row.gdl_paths) == [str(parent)]
