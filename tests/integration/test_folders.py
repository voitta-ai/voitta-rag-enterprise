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

    r = client.post("/api/folders", json={"name": src.name})
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

    r = client.post("/api/folders", json={"name": src.name})
    folder_id = r.json()["id"]

    files = client.get(f"/api/folders/{folder_id}/files").json()
    rels = [f["rel_path"] for f in files]
    assert rels == ["keep.txt"]


def test_sidecar_source_urls_surface(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _seed(src, {"spec.md": "# spec", "code.py": "print('x')"})
    (src / ".voitta_sources.json").write_text(
        json.dumps({"spec.md": {"url": "https://example.com/docs/spec"}})
    )

    r = client.post("/api/folders", json={"name": src.name})
    folder_id = r.json()["id"]

    files = client.get(f"/api/folders/{folder_id}/files").json()
    by_path = {f["rel_path"]: f for f in files}
    assert by_path["spec.md"]["source_url"] == "https://example.com/docs/spec"
    assert by_path["code.py"]["source_url"] is None
    assert ".voitta_sources.json" not in by_path


def test_duplicate_registration_returns_409(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    assert client.post("/api/folders", json={"name": src.name}).status_code == 201
    assert client.post("/api/folders", json={"name": src.name}).status_code == 409


def test_list_and_delete_folder(client: TestClient, tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    client.post("/api/folders", json={"name": a.name})
    bid = client.post("/api/folders", json={"name": b.name}).json()["id"]

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
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]
    assert {f["rel_path"] for f in client.get(f"/api/folders/{folder_id}/files").json()} == {
        "a.txt",
        "b.txt",
    }

    (src / "a.txt").unlink()
    (src / "c.txt").write_text("c")

    from voitta_rag_enterprise.main import create_app

    new_app = create_app()
    with TestClient(new_app) as new_client:
        files = new_client.get(f"/api/folders/{folder_id}/files").json()
        states = {f["rel_path"]: f["state"] for f in files}
        assert states["a.txt"] == "deleted"
        assert states["b.txt"] == "pending"
        assert states["c.txt"] == "pending"


def test_reindex_folder_resets_files_and_enqueues_extracts(
    client: TestClient, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    _seed(src, {"a.txt": "a", "b/c.txt": "c", "b/d.txt": "d", "e.txt": "e"})
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    # Pretend everything has already been indexed once: SHA set, state=indexed,
    # plus some chunks attached so we can verify reindex wipes them.
    from sqlalchemy import select

    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import Chunk, File, Job

    with session_scope() as s:
        for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars():
            f.file_cas_id = "deadbeef" * 8
            f.state = "indexed"
            f.error = None
            for i in range(3):
                s.add(
                    Chunk(
                        file_id=f.id,
                        chunk_index=i,
                        chunk_hash=f"hash_{f.id}_{i}",
                        text=f"chunk {i}",
                        char_start=0,
                        char_end=10,
                        created_at=0,
                    )
                )

    # Sanity: chunks are there before reindex.
    with session_scope() as s:
        before = s.execute(select(Chunk).where(Chunk.file_id.in_(
            [r[0] for r in s.execute(select(File.id).where(File.folder_id == folder_id)).all()]
        ))).scalars().all()
        assert len(before) == 12  # 4 files * 3 chunks

    r = client.post(f"/api/folders/{folder_id}/reindex", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["folder_id"] == folder_id
    assert body["rel_dir"] == ""
    assert body["scheduled"] == 4
    assert isinstance(body["job_id"], int)

    # Reindex is async (queued for the worker). The endpoint returns
    # immediately so we drive the handler directly — exactly what the
    # worker would do when it dequeues this job.
    import asyncio

    from voitta_rag_enterprise.services.indexing import run_reindex_folder

    with session_scope() as s:
        file_ids = [
            fid
            for (fid,) in s.execute(
                select(File.id).where(File.folder_id == folder_id)
            ).all()
        ]
    asyncio.run(
        run_reindex_folder(
            {"folder_id": folder_id, "rel_dir": "", "file_ids": file_ids}
        )
    )

    # Reindex must wipe every chunk row attached to those files.
    with session_scope() as s:
        after = s.execute(select(Chunk).where(Chunk.file_id.in_(
            [r[0] for r in s.execute(select(File.id).where(File.folder_id == folder_id)).all()]
        ))).scalars().all()
        assert after == [], f"reindex left {len(after)} chunk(s) behind"

    with session_scope() as s:
        files = list(
            s.execute(select(File).where(File.folder_id == folder_id)).scalars()
        )
        assert {f.rel_path for f in files} == {"a.txt", "b/c.txt", "b/d.txt", "e.txt"}
        for f in files:
            assert f.file_cas_id is None
            assert f.state == "pending"
            assert f.error is None
        # One extract job per file in queued state.
        live = list(
            s.execute(
                select(Job).where(
                    Job.kind == "extract",
                    Job.state.in_(("queued", "running")),
                )
            ).scalars()
        )
        live_payloads = sorted(json.loads(j.payload)["file_id"] for j in live)
        assert live_payloads == sorted(f.id for f in files)


def test_reindex_subdir_scopes_to_subtree(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _seed(src, {"top.txt": "t", "b/c.txt": "c", "b/d.txt": "d", "z/x.txt": "x"})
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    from sqlalchemy import select

    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import File

    with session_scope() as s:
        for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars():
            f.file_cas_id = "deadbeef" * 8
            f.state = "indexed"

    r = client.post(f"/api/folders/{folder_id}/reindex", json={"rel_dir": "b"})
    assert r.status_code == 200
    body = r.json()
    assert body["folder_id"] == folder_id
    assert body["rel_dir"] == "b"
    assert body["scheduled"] == 2

    # Drive the queued reindex job through the handler (worker is disabled
    # in tests via VOITTA_DISABLE_BACKGROUND).
    import asyncio

    from voitta_rag_enterprise.services.indexing import run_reindex_folder

    with session_scope() as s:
        # rel_dir="b" → match files under the b/ subtree only.
        file_ids = [
            fid
            for (fid,) in s.execute(
                select(File.id).where(
                    File.folder_id == folder_id,
                    File.rel_path.like("b/%"),
                )
            ).all()
        ]
    asyncio.run(
        run_reindex_folder(
            {"folder_id": folder_id, "rel_dir": "b", "file_ids": file_ids}
        )
    )

    with session_scope() as s:
        states = {
            f.rel_path: (f.file_cas_id, f.state)
            for f in s.execute(
                select(File).where(File.folder_id == folder_id)
            ).scalars()
        }
    assert states["b/c.txt"] == (None, "pending")
    assert states["b/d.txt"] == (None, "pending")
    # Outside the subtree must remain untouched.
    assert states["top.txt"][1] == "indexed"
    assert states["z/x.txt"][1] == "indexed"


def test_reindex_rejects_path_traversal(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _seed(src, {"a.txt": "a"})
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    r = client.post(f"/api/folders/{folder_id}/reindex", json={"rel_dir": "../etc"})
    assert r.status_code == 400


def test_reindex_unknown_folder_returns_404(client: TestClient) -> None:
    r = client.post("/api/folders/9999/reindex", json={})
    assert r.status_code == 404


def test_sync_get_returns_null_when_unconfigured(
    client: TestClient, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]
    r = client.get(f"/api/folders/{folder_id}/sync")
    assert r.status_code == 200
    assert r.json() is None


def test_sync_put_rejects_non_empty_folder(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    _seed(src, {"already.txt": "existing"})
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    body = {
        "source_type": "github",
        "github": {
            "repo": "https://github.com/example/repo",
            "branches": ["main"],
            "auth_method": "ssh",
        },
    }
    r = client.put(f"/api/folders/{folder_id}/sync", json=body)
    assert r.status_code == 400
    assert "empty" in r.text.lower()


def test_sync_put_then_get_round_trips(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    body = {
        "source_type": "github",
        "github": {
            "repo": "https://github.com/example/repo",
            "path": "docs",
            "branches": ["main", "develop"],
            "all_branches": False,
            "extended": True,
            "auth_method": "token",
            "username": "x-access-token",
            "pat": "ghp_secret_abc",
        },
    }
    r = client.put(f"/api/folders/{folder_id}/sync", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["source_type"] == "github"
    gh = out["github"]
    assert gh["repo"] == "https://github.com/example/repo"
    assert gh["branches"] == ["main", "develop"]
    assert gh["extended"] is True
    assert gh["auth_method"] == "token"
    assert gh["has_pat"] is True
    assert gh["has_ssh_key"] is False

    # Reading back should not echo the token.
    r2 = client.get(f"/api/folders/{folder_id}/sync").json()
    assert r2["github"]["has_pat"] is True
    assert "pat" not in r2["github"]


def test_sync_put_rejects_no_branch_selection(
    client: TestClient, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    r = client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "github",
            "github": {
                "repo": "https://github.com/example/repo",
                "branches": [],
                "all_branches": False,
            },
        },
    )
    assert r.status_code == 400


def test_sync_reconfig_allowed_after_files_present(
    client: TestClient, tmp_path: Path
) -> None:
    """Once a source exists, the empty-folder check no longer applies — users
    must be able to add/remove branches even after the first sync has populated
    the folder."""
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    body = {
        "source_type": "github",
        "github": {"repo": "https://x/r", "branches": ["main"], "auth_method": "ssh"},
    }
    assert client.put(f"/api/folders/{folder_id}/sync", json=body).status_code == 200

    # Drop a file simulating a successful first sync.
    (src / "branches").mkdir()
    (src / "branches" / "main").mkdir()
    (src / "branches" / "main" / "README.md").write_text("hi")

    body["github"]["branches"] = ["main", "develop"]
    r = client.put(f"/api/folders/{folder_id}/sync", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["github"]["branches"] == ["main", "develop"]


def test_sync_trigger_enqueues_job(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "github",
            "github": {"repo": "https://x/r", "branches": ["main"], "auth_method": "ssh"},
        },
    )
    r = client.post(f"/api/folders/{folder_id}/sync/trigger")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["folder_id"] == folder_id
    assert isinstance(body["job_id"], int)

    # Triggering twice should dedup to the same job (queued state).
    r2 = client.post(f"/api/folders/{folder_id}/sync/trigger")
    assert r2.status_code == 200
    assert r2.json()["job_id"] == body["job_id"]


def test_sync_trigger_without_source_returns_400(
    client: TestClient, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]
    r = client.post(f"/api/folders/{folder_id}/sync/trigger")
    assert r.status_code == 400


def test_folder_listing_exposes_has_sync_source_flag(
    client: TestClient, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    folders = client.get("/api/folders").json()
    me = next(f for f in folders if f["id"] == folder_id)
    assert me["has_sync_source"] is False

    client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "github",
            "github": {"repo": "https://x/r", "branches": ["main"], "auth_method": "ssh"},
        },
    )
    me = next(f for f in client.get("/api/folders").json() if f["id"] == folder_id)
    assert me["has_sync_source"] is True

    client.delete(f"/api/folders/{folder_id}/sync")
    me = next(f for f in client.get("/api/folders").json() if f["id"] == folder_id)
    assert me["has_sync_source"] is False


def test_sync_delete_removes_source(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]
    client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "github",
            "github": {"repo": "https://x/r", "branches": ["main"], "auth_method": "ssh"},
        },
    )
    assert client.delete(f"/api/folders/{folder_id}/sync").status_code == 204
    assert client.get(f"/api/folders/{folder_id}/sync").json() is None


def test_unauthenticated_request_returns_401(env: None, tmp_path: Path) -> None:
    """With no auth mode set, every API call needs a Google session."""
    from voitta_rag_enterprise.main import create_app

    from ..conftest import auth_as

    app = create_app()
    with TestClient(app) as anon:
        r = anon.get("/api/folders")
        assert r.status_code == 401

    # Once a session is in place (auth_as overrides the dependency to mimic
    # the post-OAuth state), the same endpoint becomes 200.
    app2 = create_app()
    auth_as(app2, "alice@x.com")
    with TestClient(app2) as client:
        r = client.get("/api/folders")
        assert r.status_code == 200
