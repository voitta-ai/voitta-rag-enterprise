"""POST /api/sync/link/connect and the in-place guards it relies on.

End-to-end through the FastAPI app: admin sets the linked-folder root, a
folder is created (managed dir + watch), then linked — after which the
managed dir is gone, ``folder.path`` is the linked directory, and every
write-shaped route refuses. The linked tree must be byte-for-byte untouched
throughout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voitta_rag_enterprise.services import admin_store, events


def _snapshot(root: Path) -> list[tuple[str, int]]:
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size if p.is_file() else -1)
        for p in root.rglob("*")
    )


@pytest.fixture
def link_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An admin link root that is DISJOINT from everything the app writes.

    ``conftest.env`` points ``VOITTA_DATA_DIR`` at ``tmp_path/data`` and
    ``VOITTA_ROOT_PATH`` at ``tmp_path``; the linked tree must live outside
    both, or the app's own DB / logs / managed dirs pollute the byte-for-byte
    snapshots these tests take of it.
    """
    settings_dir = tmp_path / "admin"
    settings_dir.mkdir()
    root = tmp_path.parent / f"{tmp_path.name}-linkroot"
    lake = root / "ai-news" / "sources" / "labs" / "acme"
    lake.mkdir(parents=True)
    (lake / "content.md").write_text("# hello")
    (lake / "raw.html").write_text("<html>x</html>")
    (root / "ai-news" / "_blobs").mkdir()
    (root / "ai-news" / "_blobs" / "x.bin").write_bytes(b"\x00")
    monkeypatch.setattr(admin_store, "admin_dir", lambda: settings_dir)
    admin_store.save_settings({"link_root": str(root)})
    return root


def _mk_folder(client: TestClient, name: str) -> tuple[int, str]:
    r = client.post("/api/folders", json={"name": name})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["id"], body["path"]


# ---------------------------------------------------------------------------
# capability + browse
# ---------------------------------------------------------------------------


def test_status_disabled_without_root(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    settings_dir = tmp_path / "admin"
    settings_dir.mkdir()
    monkeypatch.setattr(admin_store, "admin_dir", lambda: settings_dir)
    r = client.get("/api/sync/link/status")
    assert r.json() == {"available": False, "status": "disabled", "link_root": ""}


def test_status_and_browse(client: TestClient, link_root: Path) -> None:
    r = client.get("/api/sync/link/status")
    assert r.json() == {"available": True, "status": "ok", "link_root": str(link_root)}

    r = client.get("/api/sync/link/browse", params={"rel": ""})
    body = r.json()
    assert body["parent"] is None
    assert [e["name"] for e in body["entries"]] == ["ai-news"]

    r = client.get("/api/sync/link/browse", params={"rel": "ai-news"})
    body = r.json()
    assert body["parent"] == ""
    assert body["path"] == str(link_root / "ai-news")
    assert [e["rel_path"] for e in body["entries"]] == ["ai-news/_blobs", "ai-news/sources"]


def test_browse_rejects_escape(client: TestClient, link_root: Path) -> None:
    assert client.get("/api/sync/link/browse", params={"rel": "../etc"}).status_code == 400
    assert client.get("/api/sync/link/browse", params={"rel": "nope"}).status_code == 404


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_links_in_place(client: TestClient, link_root: Path, monkeypatch) -> None:
    lake = link_root / "ai-news" / "sources"
    before = _snapshot(link_root)
    fid, managed_path = _mk_folder(client, "ai-news")
    assert Path(managed_path).is_dir()

    published: list[dict] = []
    monkeypatch.setattr(events, "publish", lambda topic, ev: published.append(ev))

    r = client.post(
        "/api/sync/link/connect",
        json={
            "folder_id": fid,
            "rel_path": "ai-news/sources",
            "ignore": ["_blobs", "raw.html", ""],
            "auto_sync_enabled": True,
            "auto_sync_hours": 2,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["folder_id"] == fid

    # The empty managed directory is gone; the folder IS the linked directory.
    assert not Path(managed_path).exists()
    got = client.get(f"/api/folders/{fid}/sync").json()
    assert got["source_type"] == "local_link"
    assert got["auto_sync_enabled"] is True and got["auto_sync_hours"] == 2
    ll = got["local_link"]
    assert ll["path"] == str(lake)
    assert ll["rel_path"] == "ai-news/sources"
    assert ll["ignore"] == ["_blobs", "raw.html"]
    assert ll["available"] is True and ll["status"] == "ok"

    folders = {f["id"]: f for f in client.get("/api/folders").json()}
    assert folders[fid]["path"] == str(lake)
    assert folders[fid]["source_type"] == "local_link"
    assert folders[fid]["sync_source_kind"] == "local_link"

    # Live-update contract: full config + field-complete status event.
    by_type = {ev["type"]: ev for ev in published}
    assert by_type["folder.sync_config_changed"]["config"]["local_link"]["path"] == str(lake)
    assert by_type["folder.upserted"]["folder"]["path"] == str(lake)
    assert "last_synced_at" in by_type["folder.sync_source_changed"]

    # A sync job is queued, and the linked tree is byte-for-byte untouched.
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import FolderSyncSource, Job

    with session_scope() as s:
        row = s.get(FolderSyncSource, fid)
        assert row.ll_path == str(lake)
        assert json.loads(row.ll_ignore) == ["_blobs", "raw.html"]
        jobs = [j for j in s.query(Job).all() if j.kind == "sync"]
        assert len(jobs) == 1
    assert _snapshot(link_root) == before


def test_connect_rejects_root_itself_and_bad_paths(client: TestClient, link_root: Path) -> None:
    fid, _ = _mk_folder(client, "f1")
    for rel, code in (("", 400), ("/", 400), ("../x", 400), ("ai-news/missing", 400)):
        r = client.post("/api/sync/link/connect", json={"folder_id": fid, "rel_path": rel})
        assert r.status_code == code, (rel, r.text)


def test_connect_rejects_separator_in_ignore(client: TestClient, link_root: Path) -> None:
    fid, _ = _mk_folder(client, "f2")
    r = client.post(
        "/api/sync/link/connect",
        json={"folder_id": fid, "rel_path": "ai-news/sources", "ignore": ["a/b"]},
    )
    assert r.status_code == 400
    assert "path separator" in r.json()["detail"]


def test_connect_refuses_non_empty_folder(client: TestClient, link_root: Path) -> None:
    fid, managed = _mk_folder(client, "f3")
    (Path(managed) / "note.txt").write_text("real file")
    r = client.post(
        "/api/sync/link/connect", json={"folder_id": fid, "rel_path": "ai-news/sources"}
    )
    assert r.status_code == 400
    assert "must start empty" in r.json()["detail"]
    assert (Path(managed) / "note.txt").exists()  # nothing was removed


def test_connect_refuses_duplicate_link(client: TestClient, link_root: Path) -> None:
    a, _ = _mk_folder(client, "a")
    b, _ = _mk_folder(client, "b")
    assert client.post(
        "/api/sync/link/connect", json={"folder_id": a, "rel_path": "ai-news/sources"}
    ).status_code == 200
    r = client.post(
        "/api/sync/link/connect", json={"folder_id": b, "rel_path": "ai-news/sources"}
    )
    assert r.status_code == 409
    assert "already linked" in r.json()["detail"]


def test_reconnect_replaces_ignore_list(client: TestClient, link_root: Path) -> None:
    fid, _ = _mk_folder(client, "re")
    body = {"folder_id": fid, "rel_path": "ai-news/sources", "ignore": ["_blobs"]}
    assert client.post("/api/sync/link/connect", json=body).status_code == 200
    body["ignore"] = ["raw.html"]
    assert client.post("/api/sync/link/connect", json=body).status_code == 200
    assert client.get(f"/api/folders/{fid}/sync").json()["local_link"]["ignore"] == ["raw.html"]


def test_connect_disabled_when_root_unset(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    settings_dir = tmp_path / "admin"
    settings_dir.mkdir()
    monkeypatch.setattr(admin_store, "admin_dir", lambda: settings_dir)
    fid, _ = _mk_folder(client, "nolink")
    r = client.post("/api/sync/link/connect", json={"folder_id": fid, "rel_path": "x"})
    assert r.status_code == 400
    assert "not available" in r.json()["detail"]


# ---------------------------------------------------------------------------
# guards on a linked folder
# ---------------------------------------------------------------------------


@pytest.fixture
def linked(client: TestClient, link_root: Path) -> tuple[int, Path]:
    fid, _ = _mk_folder(client, "guarded")
    r = client.post(
        "/api/sync/link/connect", json={"folder_id": fid, "rel_path": "ai-news/sources"}
    )
    assert r.status_code == 200, r.text
    return fid, link_root / "ai-news" / "sources"


def test_writes_are_refused(client: TestClient, linked: tuple[int, Path]) -> None:
    fid, lake = linked
    before = _snapshot(lake)

    # Every write-shaped route refuses a folder with a sync source (409 from
    # _require_regular) — a linked folder always has one.
    r = client.post(
        f"/api/folders/{fid}/upload",
        files={"file": ("evil.txt", b"x", "text/plain")},
    )
    assert r.status_code == 409
    assert client.post(f"/api/folders/{fid}/mkdir", json={"path": "new"}).status_code == 409
    assert client.delete(f"/api/folders/{fid}/dirs", params={"rel": "labs"}).status_code == 409
    assert client.get(f"/api/folders/{fid}/dirs").json() == []  # no ghost dirs

    assert _snapshot(lake) == before


def test_sync_source_cannot_be_removed_alone(client: TestClient, linked: tuple[int, Path]) -> None:
    fid, _ = linked
    r = client.delete(f"/api/folders/{fid}/sync")
    assert r.status_code == 409
    assert client.get(f"/api/folders/{fid}/sync").json()["source_type"] == "local_link"


def test_delete_folder_leaves_the_tree(client: TestClient, linked: tuple[int, Path]) -> None:
    fid, lake = linked
    before = _snapshot(lake)
    assert client.delete(f"/api/folders/{fid}").status_code == 204
    assert client.get(f"/api/folders/{fid}/sync").status_code == 404
    assert _snapshot(lake) == before


def test_trigger_reports_missing_directory(client: TestClient, linked: tuple[int, Path]) -> None:
    import shutil

    fid, lake = linked
    shutil.rmtree(lake)
    r = client.post(f"/api/folders/{fid}/sync/trigger")
    assert r.status_code == 400
    assert "missing" in r.json()["detail"]
    assert client.get(f"/api/folders/{fid}/sync").json()["local_link"]["status"] == "missing"


# ---------------------------------------------------------------------------
# admin setting
# ---------------------------------------------------------------------------


def test_admin_link_root_roundtrip(app, tmp_path: Path, monkeypatch) -> None:
    from tests.integration.test_admin_endpoints import _make_super_admin

    settings_dir = tmp_path / "admin"
    settings_dir.mkdir()
    monkeypatch.setattr(admin_store, "admin_dir", lambda: settings_dir)
    good = tmp_path / "good"
    good.mkdir()
    _make_super_admin(app, "boss@example.com", monkeypatch)

    with TestClient(app) as c:
        r = c.patch("/api/admin/settings", json={"link_root": str(good)})
        assert r.status_code == 200, r.text
        assert r.json()["link_root"] == str(good)
        assert r.json()["link_available"] is True and r.json()["link_status"] == "ok"
        # NFS root untouched by a link_root write.
        assert r.json()["nfs_root"] == ""

        r = c.patch("/api/admin/settings", json={"link_root": str(tmp_path / "missing")})
        assert r.status_code == 400
        assert "Linked-folder root" in r.json()["detail"]

        r = c.patch("/api/admin/settings", json={"link_root": ""})
        assert r.status_code == 200
        assert r.json()["link_status"] == "disabled"

        # The sync-side probe reads the same setting.
        c.patch("/api/admin/settings", json={"link_root": str(good)})
        assert c.get("/api/sync/link/status").json()["link_root"] == str(good)
