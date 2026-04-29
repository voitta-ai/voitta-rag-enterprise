"""Integration tests for folder registration + reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient


def _seed(root: Path, layout: dict[str, str]) -> None:
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_register_folder_lists_files(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _seed(src, {"a.txt": "alpha", "b/c.txt": "charlie", "b/d.txt": "delta"})

    r = client.post("/api/folders", json={"path": str(src)})
    assert r.status_code == 201, r.text
    folder = r.json()
    assert folder["display_name"] == "src"
    assert folder["source_type"] == "filesystem"

    files = client.get(f"/api/folders/{folder['id']}/files").json()
    rels = sorted(f["rel_path"] for f in files)
    assert rels == ["a.txt", "b/c.txt", "b/d.txt"]
    for f in files:
        assert f["state"] == "pending"
        assert f["size_bytes"] is not None


def test_ignored_patterns_excluded(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _seed(
        src,
        {
            "keep.txt": "x",
            ".git/HEAD": "ref",
            "node_modules/lodash/index.js": "// js",
            "a/__pycache__/foo.pyc": "bytes",
            "a/.DS_Store": "",
        },
    )

    r = client.post("/api/folders", json={"path": str(src)})
    folder_id = r.json()["id"]

    files = client.get(f"/api/folders/{folder_id}/files").json()
    rels = [f["rel_path"] for f in files]
    assert rels == ["keep.txt"]


def test_sidecar_source_urls_surface(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _seed(src, {"spec.md": "# spec", "code.py": "print('x')"})
    (src / ".voitta_sources.json").write_text(
        json.dumps({"spec.md": "https://example.com/docs/spec"})
    )

    r = client.post("/api/folders", json={"path": str(src)})
    folder_id = r.json()["id"]

    files = client.get(f"/api/folders/{folder_id}/files").json()
    by_path = {f["rel_path"]: f for f in files}
    assert by_path["spec.md"]["source_url"] == "https://example.com/docs/spec"
    assert by_path["code.py"]["source_url"] is None
    assert ".voitta_sources.json" not in by_path


def test_register_nonexistent_path_returns_400(client: TestClient, tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist"
    r = client.post("/api/folders", json={"path": str(bogus)})
    assert r.status_code == 400


def test_duplicate_registration_returns_409(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    assert client.post("/api/folders", json={"path": str(src)}).status_code == 201
    assert client.post("/api/folders", json={"path": str(src)}).status_code == 409


def test_list_and_delete_folder(client: TestClient, tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    client.post("/api/folders", json={"path": str(a)})
    bid = client.post("/api/folders", json={"path": str(b)}).json()["id"]

    folders = client.get("/api/folders").json()
    assert len(folders) == 2

    r = client.delete(f"/api/folders/{bid}")
    assert r.status_code == 204
    assert client.get(f"/api/folders/{bid}/files").status_code == 404

    folders = client.get("/api/folders").json()
    assert len(folders) == 1


def test_list_files_unknown_folder_404(client: TestClient) -> None:
    assert client.get("/api/folders/9999/files").status_code == 404


def test_restart_picks_up_new_files_and_marks_vanished(
    client: TestClient, tmp_path: Path
) -> None:
    """Re-creating the app simulates a restart; the lifespan scan reconciles."""
    src = tmp_path / "src"
    _seed(src, {"a.txt": "a", "b.txt": "b"})
    folder_id = client.post("/api/folders", json={"path": str(src)}).json()["id"]
    assert {f["rel_path"] for f in client.get(f"/api/folders/{folder_id}/files").json()} == {
        "a.txt",
        "b.txt",
    }

    (src / "a.txt").unlink()
    (src / "c.txt").write_text("c")

    from voitta_image_rag.main import create_app

    new_app = create_app()
    with TestClient(new_app) as new_client:
        files = new_client.get(f"/api/folders/{folder_id}/files").json()
        states = {f["rel_path"]: f["state"] for f in files}
        assert states["a.txt"] == "deleted"
        assert states["b.txt"] == "pending"
        assert states["c.txt"] == "pending"


def test_unauthenticated_request_returns_401(env: None, tmp_path: Path) -> None:
    """With no auth mode set, every API call needs an X-Forwarded-Email header."""
    from voitta_image_rag.main import create_app

    app = create_app()
    with TestClient(app) as anon:
        r = anon.get("/api/folders")
        assert r.status_code == 401
        r = anon.get("/api/folders", headers={"X-Forwarded-Email": "alice@x.com"})
        assert r.status_code == 200
