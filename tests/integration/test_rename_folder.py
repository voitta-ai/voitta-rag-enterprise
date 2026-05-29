"""Tests for PATCH /api/folders/{id}/rename.

Covers the two rename modes ("Both" semantics): display-name-only (cheap,
no disk touch) and physical rename (moves the dir under VOITTA_ROOT_PATH +
rewrites folders.path). The load-bearing invariants asserted here:

  * a physical rename moves the dir and its files, preserves File rows
    (same folder_id, same rel_path), and rewrites folders.path;
  * name collisions and invalid names are refused (409 / 400);
  * an in-flight job blocks a physical rename (409) but never blocks a
    label-only edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def root_env(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from voitta_rag_enterprise.config import reset_settings_cache

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("VOITTA_ROOT_PATH", str(root))
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    reset_settings_cache()
    return root


def _client(root_env: Path) -> TestClient:
    from voitta_rag_enterprise.main import create_app

    return TestClient(create_app())


def test_display_name_only_leaves_disk_untouched(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "proj"}).json()["id"]
        r = c.patch(f"/api/folders/{fid}/rename", json={"display_name": "My Project"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["display_name"] == "My Project"
        # Physical dir + path unchanged.
        assert body["path"] == str(root_env / "proj")
        assert (root_env / "proj").is_dir()


def test_physical_rename_moves_dir_and_preserves_files(root_env: Path) -> None:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import File

    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "old-name"}).json()["id"]
        c.post(
            f"/api/folders/{fid}/upload",
            files={"file": ("doc.txt", b"hello", "text/plain")},
        )
        # Stand in for the scanner/watcher (not reliably running under the
        # TestClient): create the File row by hand so we can prove it
        # survives the rename byte-for-byte.
        with session_scope() as s:
            row = File(folder_id=fid, rel_path="doc.txt", state="indexed")
            s.add(row)
            s.flush()
            file_id, rel_before = row.id, row.rel_path

        r = c.patch(f"/api/folders/{fid}/rename", json={"name": "new-name"})
        assert r.status_code == 200, r.text
        assert r.json()["path"] == str(root_env / "new-name")

        # Dir physically moved, file rode along.
        assert not (root_env / "old-name").exists()
        assert (root_env / "new-name" / "doc.txt").read_bytes() == b"hello"

        # File row is the SAME row — folder_id and (relative) rel_path
        # are rename-invariant, so no re-index is implied.
        with session_scope() as s:
            after = s.get(File, file_id)
            assert after is not None
            assert after.folder_id == fid
            assert after.rel_path == rel_before


def test_rename_both_fields_at_once(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "raw"}).json()["id"]
        r = c.patch(
            f"/api/folders/{fid}/rename",
            json={"display_name": "Polished", "name": "polished"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["display_name"] == "Polished"
        assert body["path"] == str(root_env / "polished")


def test_rename_empty_body_is_400(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "x"}).json()["id"]
        r = c.patch(f"/api/folders/{fid}/rename", json={})
        assert r.status_code == 400


def test_rename_to_existing_name_conflicts(root_env: Path) -> None:
    with _client(root_env) as c:
        a = c.post("/api/folders", json={"name": "alpha"}).json()["id"]
        c.post("/api/folders", json={"name": "beta"})
        # Renaming alpha -> beta collides with the registered beta folder.
        r = c.patch(f"/api/folders/{a}/rename", json={"name": "beta"})
        assert r.status_code == 409


def test_rename_to_existing_dir_conflicts(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "src"}).json()["id"]
        # A bare directory exists at the target but isn't registered.
        (root_env / "taken").mkdir()
        r = c.patch(f"/api/folders/{fid}/rename", json={"name": "taken"})
        assert r.status_code == 409
        # Original dir untouched.
        assert (root_env / "src").is_dir()


def test_rename_invalid_name_rejected(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "ok"}).json()["id"]
        for bad in ["../escape", "a/b", "..", ".hidden"]:
            r = c.patch(f"/api/folders/{fid}/rename", json={"name": bad})
            assert r.status_code == 400, f"{bad!r} should be 400, got {r.status_code}"


def test_active_job_blocks_physical_rename_but_not_label(root_env: Path) -> None:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.services import job_queue

    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "busy"}).json()["id"]
        # Enqueue a folder-level job so the folder reads as "in flight".
        with session_scope() as s:
            job_queue.enqueue(s, "reindex_folder", {"folder_id": fid})

        # Physical rename refused while work is queued.
        r = c.patch(f"/api/folders/{fid}/rename", json={"name": "renamed"})
        assert r.status_code == 409
        assert (root_env / "busy").is_dir()

        # Label-only edit is always allowed — it never touches disk.
        r = c.patch(f"/api/folders/{fid}/rename", json={"display_name": "Busy One"})
        assert r.status_code == 200, r.text
        assert r.json()["display_name"] == "Busy One"


def test_rename_outside_root_refused(root_env: Path, tmp_path: Path) -> None:
    """Hand-edit the DB to point a folder outside root; a physical rename
    must refuse to move it (mirrors the delete-folder disk guard)."""
    from sqlalchemy import update

    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import Folder

    outside = tmp_path / "external"
    outside.mkdir()

    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "redirected"}).json()["id"]
        with session_scope() as s:
            s.execute(update(Folder).where(Folder.id == fid).values(path=str(outside)))

        r = c.patch(f"/api/folders/{fid}/rename", json={"name": "elsewhere"})
        assert r.status_code == 400
        assert outside.exists()
