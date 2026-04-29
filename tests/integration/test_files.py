"""Integration tests for the file detail endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_get_file_returns_detail(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    folder_id = client.post("/api/folders", json={"path": str(src)}).json()["id"]
    files = client.get(f"/api/folders/{folder_id}/files").json()
    file_id = files[0]["id"]

    r = client.get(f"/api/files/{file_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == file_id
    assert body["rel_path"] == "a.txt"
    assert body["folder_id"] == folder_id
    assert body["state"] == "pending"
    assert body["pending_embeds"] == 0


def test_get_file_unknown_returns_404(client: TestClient) -> None:
    assert client.get("/api/files/9999").status_code == 404
