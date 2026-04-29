"""Tests for the read-only filesystem browser endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_list_default_returns_home(client: TestClient) -> None:
    r = client.get("/api/fs/list")
    assert r.status_code == 200
    body = r.json()
    assert body["path"]
    assert "entries" in body


def test_list_explicit_path(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "file.txt").write_text("x")

    r = client.get(f"/api/fs/list?path={tmp_path}")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(tmp_path)
    names = [e["name"] for e in body["entries"]]
    assert names[0:2] == ["alpha", "beta"]  # dirs sorted first
    assert "file.txt" in names
    file_entry = next(e for e in body["entries"] if e["name"] == "file.txt")
    assert file_entry["is_dir"] is False


def test_list_parent_set_correctly(client: TestClient, tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    r = client.get(f"/api/fs/list?path={sub}")
    assert r.json()["parent"] == str(tmp_path)


def test_list_root_has_no_parent(client: TestClient) -> None:
    r = client.get("/api/fs/list?path=/")
    assert r.json()["parent"] is None


def test_list_404_for_missing_path(client: TestClient, tmp_path: Path) -> None:
    r = client.get(f"/api/fs/list?path={tmp_path / 'nope'}")
    assert r.status_code == 404


def test_list_400_for_a_file(client: TestClient, tmp_path: Path) -> None:
    p = tmp_path / "x.txt"
    p.write_text("x")
    r = client.get(f"/api/fs/list?path={p}")
    assert r.status_code == 400


def test_list_requires_auth(env: None, tmp_path: Path) -> None:
    from voitta_image_rag.main import create_app

    app = create_app()
    with TestClient(app) as anon:
        r = anon.get("/api/fs/list")
        assert r.status_code == 401
