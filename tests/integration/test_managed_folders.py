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
        assert body["managed"] is True
        assert body["path"] == str(root_env / "project-x")
        assert (root_env / "project-x").is_dir()


def test_create_managed_uses_existing_dir(root_env: Path) -> None:
    (root_env / "preexisting").mkdir()
    with _client(root_env) as c:
        r = c.post("/api/folders", json={"name": "preexisting"})
        assert r.status_code == 201
        assert r.json()["managed"] is True


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


def test_must_provide_exactly_one_of_path_or_name(root_env: Path, tmp_path: Path) -> None:
    with _client(root_env) as c:
        # Neither
        r = c.post("/api/folders", json={})
        assert r.status_code == 400
        # Both
        ext = tmp_path / "ext"
        ext.mkdir()
        r = c.post("/api/folders", json={"path": str(ext), "name": "x"})
        assert r.status_code == 400


def test_external_folder_is_not_managed(root_env: Path, tmp_path: Path) -> None:
    ext = tmp_path / "external"
    ext.mkdir()
    with _client(root_env) as c:
        r = c.post("/api/folders", json={"path": str(ext)})
        assert r.status_code == 201
        assert r.json()["managed"] is False


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


def test_upload_to_external_folder_rejected(root_env: Path, tmp_path: Path) -> None:
    ext = tmp_path / "external"
    ext.mkdir()
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"path": str(ext)}).json()["id"]
        r = c.post(
            f"/api/folders/{fid}/upload",
            files={"file": ("x.txt", b"x", "text/plain")},
        )
        assert r.status_code == 400


def test_upload_path_traversal_rejected(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u2"}).json()["id"]
        r = c.post(
            f"/api/folders/{fid}/upload?rel_path=../escape.txt",
            files={"file": ("x.txt", b"x", "text/plain")},
        )
        assert r.status_code == 400


def test_listing_includes_managed_flag(root_env: Path, tmp_path: Path) -> None:
    ext = tmp_path / "external"
    ext.mkdir()
    with _client(root_env) as c:
        c.post("/api/folders", json={"name": "managed-one"})
        c.post("/api/folders", json={"path": str(ext)})
        rows = c.get("/api/folders").json()
        flags = {f["display_name"]: f["managed"] for f in rows}
        assert flags["managed-one"] is True
        assert flags["external"] is False
