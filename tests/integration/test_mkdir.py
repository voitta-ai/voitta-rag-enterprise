"""Tests for POST /api/folders/{id}/mkdir."""

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


def test_mkdir_creates_subdir(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u"}).json()["id"]
        r = c.post(f"/api/folders/{fid}/mkdir", json={"path": "designs"})
        assert r.status_code == 201
        assert r.json() == {"rel_path": "designs"}
        assert (root_env / "u" / "designs").is_dir()


def test_mkdir_nested(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u"}).json()["id"]
        r = c.post(f"/api/folders/{fid}/mkdir", json={"path": "designs/v2/draft"})
        assert r.status_code == 201
        assert (root_env / "u" / "designs" / "v2" / "draft").is_dir()


def test_mkdir_idempotent(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u"}).json()["id"]
        c.post(f"/api/folders/{fid}/mkdir", json={"path": "x"})
        r = c.post(f"/api/folders/{fid}/mkdir", json={"path": "x"})
        assert r.status_code == 201


def test_mkdir_path_traversal_rejected(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u"}).json()["id"]
        for bad in ["../escape", "..", "a/../../b"]:
            r = c.post(f"/api/folders/{fid}/mkdir", json={"path": bad})
            assert r.status_code == 400, f"{bad!r} → {r.status_code}"


def test_mkdir_strips_leading_slash(root_env: Path) -> None:
    """A leading slash is treated as relative, not an escape attempt."""
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u"}).json()["id"]
        r = c.post(f"/api/folders/{fid}/mkdir", json={"path": "/abs"})
        assert r.status_code == 201
        assert (root_env / "u" / "abs").is_dir()


def test_mkdir_unknown_folder_returns_404(root_env: Path) -> None:
    with _client(root_env) as c:
        r = c.post("/api/folders/9999/mkdir", json={"path": "x"})
        assert r.status_code == 404


def test_upload_into_subdir_writes_under_it(root_env: Path) -> None:
    with _client(root_env) as c:
        fid = c.post("/api/folders", json={"name": "u"}).json()["id"]
        c.post(f"/api/folders/{fid}/mkdir", json={"path": "sub"})
        r = c.post(
            f"/api/folders/{fid}/upload?rel_path=sub/hello.txt",
            files={"file": ("hello.txt", b"hi", "text/plain")},
        )
        assert r.status_code == 201
        assert (root_env / "u" / "sub" / "hello.txt").read_bytes() == b"hi"
