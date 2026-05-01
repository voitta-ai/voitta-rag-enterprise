"""Tests for folder ownership, sharing, and per-user MCP-search opt-out.

These exercise the new authorisation surface: owner-only mutations,
shared-folder visibility, and the ``active`` toggle's effect on MCP search.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import (
    File,
    Folder,
    FolderUserSettings,
)
from voitta_image_rag.services.acl import (
    folder_active_for_user,
    is_folder_owner,
    mcp_visible_folder_ids,
    visible_folder_ids,
)

from ..conftest import auth_as


def _seed(root: Path, layout: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _app():
    from voitta_image_rag.main import create_app

    return create_app()


def _register(client: TestClient, app, email: str, path: Path) -> dict:
    auth_as(app, email)
    r = client.post("/api/folders", json={"path": str(path)})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_creator_becomes_owner(env: None, tmp_path: Path) -> None:
    src = tmp_path / "alice"
    _seed(src, {"a.md": "alpha"})
    app = _app()
    with TestClient(app) as client:
        out = _register(client, app, "alice@x", src)
    assert out["owned"] is True
    assert out["shared"] is False
    assert out["owner_id"] is not None

    with session_scope() as s:
        folder = s.execute(select(Folder)).scalar_one()
        assert folder.owner_id is not None
        assert is_folder_owner(s, folder.id, folder.owner_id)


def test_non_owner_cannot_share_or_reindex_or_delete(
    env: None, tmp_path: Path
) -> None:
    """Bob has no relationship to alice's folder. Every owner-only mutation
    must come back as 404 (folder not even visible) or 403 if visible."""
    src = tmp_path / "alice"
    _seed(src, {"a.md": "alpha"})
    app = _app()
    with TestClient(app) as client:
        a = _register(client, app, "alice@x", src)
        fid = a["id"]

        auth_as(app, "bob@x")
        # Not visible at all → 404 across the board.
        for path in (
            f"/api/folders/{fid}",  # delete
            f"/api/folders/{fid}/reindex",  # POST
        ):
            r = client.delete(path) if path.endswith(str(fid)) else client.post(
                path, json={}
            )
            assert r.status_code == 404, (path, r.text)

        r = client.patch(f"/api/folders/{fid}/share", json={"shared": True})
        assert r.status_code == 404


def test_share_makes_folder_visible_read_only_to_others(
    env: None, tmp_path: Path
) -> None:
    """When alice flips ``shared=true``, bob sees the folder in his listing
    but every owner-only mutation comes back 403 (visible-but-not-owner)."""
    src = tmp_path / "alice"
    _seed(src, {"a.md": "alpha"})
    app = _app()
    with TestClient(app) as client:
        a = _register(client, app, "alice@x", src)
        fid = a["id"]

        # Before sharing: bob can't see it.
        auth_as(app, "bob@x")
        bob_list = client.get("/api/folders").json()
        assert all(f["id"] != fid for f in bob_list)

        # Alice flips the share switch.
        auth_as(app, "alice@x")
        r = client.patch(f"/api/folders/{fid}/share", json={"shared": True})
        assert r.status_code == 200, r.text
        assert r.json()["shared"] is True

        # Now bob sees it, but it's read-only.
        auth_as(app, "bob@x")
        bob_list = client.get("/api/folders").json()
        bob_folder = next(f for f in bob_list if f["id"] == fid)
        assert bob_folder["shared"] is True
        assert bob_folder["owned"] is False
        # Bob can read files…
        files_resp = client.get(f"/api/folders/{fid}/files")
        assert files_resp.status_code == 200
        # …but not mutate.
        r = client.delete(f"/api/folders/{fid}")
        assert r.status_code == 403, r.text
        r = client.patch(f"/api/folders/{fid}/share", json={"shared": False})
        assert r.status_code == 403


def test_unshare_hides_folder_from_others(env: None, tmp_path: Path) -> None:
    src = tmp_path / "alice"
    _seed(src, {"a.md": "alpha"})
    app = _app()
    with TestClient(app) as client:
        a = _register(client, app, "alice@x", src)
        fid = a["id"]
        auth_as(app, "alice@x")
        client.patch(f"/api/folders/{fid}/share", json={"shared": True})

        auth_as(app, "bob@x")
        assert any(f["id"] == fid for f in client.get("/api/folders").json())

        auth_as(app, "alice@x")
        client.patch(f"/api/folders/{fid}/share", json={"shared": False})

        auth_as(app, "bob@x")
        assert all(f["id"] != fid for f in client.get("/api/folders").json())


# ---------------------------------------------------------------------------
# Per-user active toggle
# ---------------------------------------------------------------------------


def test_active_default_on_then_toggled_off_excludes_from_mcp_visible(
    env: None, tmp_path: Path
) -> None:
    src = tmp_path / "alice"
    _seed(src, {"a.md": "alpha"})
    app = _app()
    with TestClient(app) as client:
        a = _register(client, app, "alice@x", src)
        fid = a["id"]
        # Default-on
        with session_scope() as s:
            user_id = a["owner_id"]
            assert folder_active_for_user(s, fid, user_id) is True
            assert fid in mcp_visible_folder_ids(s, user_id)

        # Flip off via API
        r = client.patch(f"/api/folders/{fid}/active", json={"active": False})
        assert r.status_code == 200
        assert r.json()["active"] is False

        with session_scope() as s:
            assert folder_active_for_user(s, fid, user_id) is False
            assert fid not in mcp_visible_folder_ids(s, user_id)
            # SPA still sees it — visibility is independent of active.
            assert fid in visible_folder_ids(s, user_id)

        # Flip back on; the row should be deleted (default-on means no row).
        r = client.patch(f"/api/folders/{fid}/active", json={"active": True})
        assert r.status_code == 200
        with session_scope() as s:
            row = s.execute(
                select(FolderUserSettings).where(
                    FolderUserSettings.folder_id == fid,
                    FolderUserSettings.user_id == user_id,
                )
            ).scalar_one_or_none()
            assert row is None


def test_active_toggle_per_user_doesnt_affect_others(
    env: None, tmp_path: Path
) -> None:
    """Bob mutes a folder he's been granted; alice's view is unchanged."""
    from voitta_image_rag.db.models import User as _User
    from voitta_image_rag.services.acl import grant_folder

    src = tmp_path / "alice"
    _seed(src, {"a.md": "alpha"})
    app = _app()
    with TestClient(app) as client:
        a = _register(client, app, "alice@x", src)
        fid = a["id"]

        # Pre-create bob and grant him.
        auth_as(app, "bob@x")
        client.get("/api/auth/me")  # touch to create the user row
        with session_scope() as s:
            bob = s.execute(
                select(_User).where(_User.email == "bob@x")
            ).scalar_one()
            bob_id = bob.id
            grant_folder(s, fid, bob_id)
            s.commit()

        # Bob mutes.
        r = client.patch(f"/api/folders/{fid}/active", json={"active": False})
        assert r.status_code == 200

        with session_scope() as s:
            alice_id = a["owner_id"]
            assert fid in mcp_visible_folder_ids(s, alice_id)
            assert fid not in mcp_visible_folder_ids(s, bob_id)


# ---------------------------------------------------------------------------
# Search filter (REST and MCP both honour visibility)
# ---------------------------------------------------------------------------


def test_search_respects_visible_folder_set(
    env: None, tmp_path: Path
) -> None:
    """Bob searches for content that exists in alice's private folder; expect
    zero hits. Then alice shares; bob's search now returns it."""
    from voitta_image_rag.services.indexing import (
        run_embed_text,
        run_extract,
    )

    src = tmp_path / "alice"
    _seed(src, {"a.md": "extraordinary unique phrase apricot"})
    app = _app()
    with TestClient(app) as client:
        a = _register(client, app, "alice@x", src)
        fid = a["id"]
        # Drive the indexing pipeline so there are points to search.
        init_db()
        with session_scope() as s:
            for f in s.execute(
                select(File).where(File.folder_id == fid)
            ).scalars():
                fid_to_index = f.id
                break
        asyncio.run(run_extract({"file_id": fid_to_index}))
        asyncio.run(run_embed_text({"file_id": fid_to_index}))

        # Bob can't search for it before sharing.
        auth_as(app, "bob@x")
        before = client.post(
            "/api/search",
            json={"query": "apricot", "modes": ["chunks"]},
        ).json()
        assert before["chunks"] == []

        # Alice shares.
        auth_as(app, "alice@x")
        client.patch(f"/api/folders/{fid}/share", json={"shared": True})

        # Bob now finds it.
        auth_as(app, "bob@x")
        after = client.post(
            "/api/search",
            json={"query": "apricot", "modes": ["chunks"]},
        ).json()
        assert any("apricot" in h["payload"]["text"] for h in after["chunks"])


def test_caller_supplied_folder_ids_intersect_with_visible(
    env: None, tmp_path: Path
) -> None:
    """If bob asks to search alice's folder explicitly via folder_ids, the
    server must intersect with bob's visible set — not honour the request
    naively. Otherwise folder_ids would be a privilege-escalation backdoor."""
    from voitta_image_rag.services.indexing import (
        run_embed_text,
        run_extract,
    )

    src = tmp_path / "alice"
    _seed(src, {"a.md": "apricot mango"})
    app = _app()
    with TestClient(app) as client:
        a = _register(client, app, "alice@x", src)
        fid = a["id"]
        init_db()
        with session_scope() as s:
            file_id = s.execute(
                select(File.id).where(File.folder_id == fid)
            ).scalar_one()
        asyncio.run(run_extract({"file_id": file_id}))
        asyncio.run(run_embed_text({"file_id": file_id}))

        auth_as(app, "bob@x")
        r = client.post(
            "/api/search",
            json={
                "query": "apricot",
                "modes": ["chunks"],
                "folder_ids": [fid],
            },
        )
        assert r.status_code == 200
        assert r.json()["chunks"] == []


# ---------------------------------------------------------------------------
# Owner-only sync mutations
# ---------------------------------------------------------------------------


def test_non_owner_cannot_configure_sync_on_shared_folder(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared folder is read-only for non-owners — sync configuration must
    not be reachable. Otherwise a viewer could repoint the sync source."""
    monkeypatch.setenv("VOITTA_ROOT_PATH", str(tmp_path / "managed-root"))
    from voitta_image_rag.config import reset_settings_cache

    reset_settings_cache()

    app = _app()
    with TestClient(app) as client:
        auth_as(app, "alice@x")
        r = client.post("/api/folders", json={"name": "alice"})
        assert r.status_code == 201
        fid = r.json()["id"]
        client.patch(f"/api/folders/{fid}/share", json={"shared": True})

        auth_as(app, "bob@x")
        r = client.put(
            f"/api/folders/{fid}/sync",
            json={
                "source_type": "github",
                "github": {
                    "repo": "https://x/r",
                    "branches": ["main"],
                    "auth_method": "ssh",
                },
            },
        )
        assert r.status_code == 403, r.text
