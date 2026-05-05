"""Tests for the managed-folder mode of POST /api/folders."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def root_env(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from voitta_image_rag.config import reset_settings_cache

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("VOITTA_ROOT_PATH", str(root))
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    reset_settings_cache()
    return root


def _client(root_env: Path) -> TestClient:
    from voitta_image_rag.main import create_app

    return TestClient(create_app())


def test_create_managed_folder(root_env: Path) -> None:
    with _client(root_env) as c:
        r = c.post("/api/folders", json={"name": "project-x"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["path"] == str(root_env / "project-x")
        assert (root_env / "project-x").is_dir()


def test_create_managed_uses_existing_dir(root_env: Path) -> None:
    (root_env / "preexisting").mkdir()
    with _client(root_env) as c:
        r = c.post("/api/folders", json={"name": "preexisting"})
        assert r.status_code == 201


def test_managed_requires_root(env: None, monkeypatch, tmp_path: Path) -> None:
    from voitta_image_rag.config import reset_settings_cache
    from voitta_image_rag.main import create_app

    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    monkeypatch.delenv("VOITTA_ROOT_PATH", raising=False)
    reset_settings_cache()

    with TestClient(create_app()) as c:
        r = c.post("/api/folders", json={"name": "x"})
        assert r.status_code == 400
        assert "VOITTA_ROOT_PATH" in r.json()["detail"]


def test_managed_rejects_path_traversal(root_env: Path) -> None:
    with _client(root_env) as c:
        for bad in ["../escape", "a/b", "..", ".", ".hidden", "foo/../bar", ""]:
            r = c.post("/api/folders", json={"name": bad})
            assert r.status_code == 400, f"{bad!r} should be rejected, got {r.status_code}"


def test_name_is_required(root_env: Path) -> None:
    with _client(root_env) as c:
        r = c.post("/api/folders", json={})
        assert r.status_code == 422


def test_path_field_is_rejected(root_env: Path, tmp_path: Path) -> None:
    """External-path registration was removed for security; the field is no
    longer accepted by the schema."""
    ext = tmp_path / "external"
    ext.mkdir()
    with _client(root_env) as c:
        # Schema has no `path` field; sending it without `name` is a
        # missing-field error (422). Sending it alongside name doesn't
        # register anything outside VOITTA_ROOT_PATH.
        r = c.post("/api/folders", json={"path": str(ext)})
        assert r.status_code == 422
        r = c.post("/api/folders", json={"path": str(ext), "name": "y"})
        assert r.status_code == 201
        assert r.json()["path"] == str(root_env / "y")


def test_root_endpoint_reports_configuration(root_env: Path) -> None:
    with _client(root_env) as c:
        r = c.get("/api/folders/root")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is True
        assert body["root_path"] == str(root_env)


def test_root_endpoint_when_unconfigured(env: None, monkeypatch, tmp_path: Path) -> None:
    from voitta_image_rag.config import reset_settings_cache
    from voitta_image_rag.main import create_app

    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    monkeypatch.delenv("VOITTA_ROOT_PATH", raising=False)
    reset_settings_cache()

    with TestClient(create_app()) as c:
        r = c.get("/api/folders/root")
        assert r.json() == {"root_path": None, "configured": False}


def test_upload_to_managed_folder_writes_file(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u"}).json()["id"]
        r = c.post(
            f"/api/folders/{fid}/upload",
            files={"file": ("hello.txt", b"hello world", "text/plain")},
        )
        assert r.status_code == 201, r.text
        assert (root_env / "u" / "hello.txt").read_bytes() == b"hello world"


def test_upload_multiple_files_to_managed_folder(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "batch"}).json()["id"]
        r = c.post(
            f"/api/folders/{fid}/upload",
            files=[
                ("file", ("a.txt", b"alpha", "text/plain")),
                ("file", ("b.txt", b"beta", "text/plain")),
            ],
        )
        assert r.status_code == 201, r.text
        assert r.json()["count"] == 2
        assert (root_env / "batch" / "a.txt").read_bytes() == b"alpha"
        assert (root_env / "batch" / "b.txt").read_bytes() == b"beta"


def test_upload_path_traversal_rejected(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u2"}).json()["id"]
        r = c.post(
            f"/api/folders/{fid}/upload?rel_path=../escape.txt",
            files={"file": ("x.txt", b"x", "text/plain")},
        )
        assert r.status_code == 400


