"""Admin file-write access on shared folders.

The rule (services/acl ``can_write_folder``): FILE-content mutations
(upload / mkdir / dir-meta / reindex / delete file or subdir) are allowed
for the folder owner OR a person-level admin the folder is shared with.
Folder lifecycle (share / rename / delete folder) and sync config stay
owner-only. Non-admin viewers stay read-only, and admins outside the
sharing community don't even see the folder (404, not 403).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services.acl import stamp_person_admin

from ..conftest import auth_as

OWNER = "alice@x"
ADMIN = "adam@x"
VIEWER = "bob@x"
OUTSIDE_ADMIN = "eve@y"  # admin flag, but not in the native community


def _app():
    from voitta_rag_enterprise.main import create_app

    return create_app()


def _make_admin(email: str) -> None:
    with session_scope() as s:
        stamp_person_admin(s, email, True)


def _setup(client: TestClient, app) -> int:
    """Owner creates + community-shares a folder; all personas exist.

    Returns the folder id. Leaves the client authed as the owner.
    """
    # alice/adam/bob form the native community; eve@y stays outside it.
    for email in (OWNER, ADMIN, VIEWER):
        admin_store.add_allowed_user(email)
    # Create the accounts (auth_as creates on first use), flag the admins.
    for email in (ADMIN, VIEWER, OUTSIDE_ADMIN):
        auth_as(app, email)
    _make_admin(ADMIN)
    _make_admin(OUTSIDE_ADMIN)

    auth_as(app, OWNER)
    r = client.post("/api/folders", json={"name": "shared-docs"})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert client.patch(
        f"/api/folders/{fid}/share", json={"shared": True}
    ).status_code == 200
    return fid


def _upload(client: TestClient, fid: int, name: str = "note.md"):
    return client.post(
        f"/api/folders/{fid}/upload",
        files={"file": (name, b"hello", "text/plain")},
    )


def test_admin_can_write_files_in_shared_folder(env: None, tmp_path: Path) -> None:
    app = _app()
    with TestClient(app) as client:
        fid = _setup(client, app)

        auth_as(app, ADMIN)
        r = _upload(client, fid)
        assert r.status_code == 201, r.text
        assert r.json()["files"][0]["rel_path"] == "note.md"

        assert client.post(
            f"/api/folders/{fid}/mkdir", json={"path": "drafts"}
        ).status_code == 201
        assert client.put(
            f"/api/folders/{fid}/dir-meta",
            json={"subpath": "drafts", "description": "admin-curated"},
        ).status_code == 200
        assert client.post(
            f"/api/folders/{fid}/reindex", json={}
        ).status_code == 200
        assert client.delete(
            f"/api/folders/{fid}/dirs", params={"rel": "drafts"}
        ).status_code == 204


def test_non_admin_viewer_stays_read_only(env: None, tmp_path: Path) -> None:
    app = _app()
    with TestClient(app) as client:
        fid = _setup(client, app)

        auth_as(app, VIEWER)
        assert _upload(client, fid).status_code == 403
        assert client.post(
            f"/api/folders/{fid}/mkdir", json={"path": "nope"}
        ).status_code == 403
        assert client.post(
            f"/api/folders/{fid}/reindex", json={}
        ).status_code == 403


def test_admin_outside_community_gets_404(env: None, tmp_path: Path) -> None:
    """The folder isn't shared with eve (different community): the admin
    flag must not leak the folder's existence, let alone write access."""
    app = _app()
    with TestClient(app) as client:
        fid = _setup(client, app)

        auth_as(app, OUTSIDE_ADMIN)
        assert _upload(client, fid).status_code == 404
        assert client.post(
            f"/api/folders/{fid}/mkdir", json={"path": "nope"}
        ).status_code == 404


def test_admin_gets_no_folder_lifecycle_powers(env: None, tmp_path: Path) -> None:
    """Share/rename/delete-folder/sync stay owner-only for the admin."""
    app = _app()
    with TestClient(app) as client:
        fid = _setup(client, app)

        auth_as(app, ADMIN)
        assert client.patch(
            f"/api/folders/{fid}/share", json={"shared": False}
        ).status_code == 403
        assert client.patch(
            f"/api/folders/{fid}/rename", json={"name": "grabbed"}
        ).status_code == 403
        assert client.delete(f"/api/folders/{fid}").status_code == 403
        assert client.put(
            f"/api/folders/{fid}/sync",
            json={"source_type": "github", "github": {"repo": "https://github.com/x/y"}},
        ).status_code == 403


def test_writable_flag_in_folder_listing(env: None, tmp_path: Path) -> None:
    app = _app()
    with TestClient(app) as client:
        fid = _setup(client, app)

        def flags(email: str):
            auth_as(app, email)
            rows = client.get("/api/folders").json()
            row = next(f for f in rows if f["id"] == fid)
            return row["owned"], row["writable"]

        assert flags(OWNER) == (True, True)
        assert flags(ADMIN) == (False, True)
        assert flags(VIEWER) == (False, False)
        # Outside admin: not visible at all.
        auth_as(app, OUTSIDE_ADMIN)
        assert all(f["id"] != fid for f in client.get("/api/folders").json())
