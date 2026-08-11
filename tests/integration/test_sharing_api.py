"""Integration tests for the share-modal API surface.

Covers the merged people view (email shares ∪ legacy folder_acl grants),
both-store removal, group shares, the audience variants, owner gating,
and the share counts carried on FolderOut for the tree pill.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import (
    FolderAcl,
    FolderEmailAcl,
    Group,
    UserGroup,
)
from voitta_rag_enterprise.services.acl import grant_folder

from ..conftest import auth_as


def _register(client: TestClient, app, email: str, tmp_path: Path, name: str) -> dict:
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    auth_as(app, email)
    r = client.post("/api/folders", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _allow_native(*emails: str) -> None:
    from voitta_rag_enterprise.services import admin_store

    for email in emails:
        admin_store.add_allowed_user(email)


def test_sharing_is_owner_gated(client: TestClient, app, tmp_path: Path) -> None:
    folder = _register(client, app, "owner@x", tmp_path, "mine")
    auth_as(app, "stranger@x")
    r = client.get(f"/api/folders/{folder['id']}/sharing")
    assert r.status_code == 404  # not even visible → not probeable
    r = client.post(
        f"/api/folders/{folder['id']}/sharing/email", json={"email": "a@b.c"}
    )
    assert r.status_code == 404


def test_email_share_lifecycle(client: TestClient, app, tmp_path: Path) -> None:
    folder = _register(client, app, "owner@x", tmp_path, "docs")
    fid = folder["id"]

    # Add a pending (unregistered) address — appears immediately.
    r = client.post(
        f"/api/folders/{fid}/sharing/email", json={"email": "Future@X "}
    )
    assert r.status_code == 200, r.text
    people = r.json()["people"]
    assert people == [
        {
            "email": "future@x",  # normalised
            "status": "pending",
            "outside_org": False,  # owner has no community → nothing to be outside of
            "legacy": False,
        }
    ]

    # The tree pill counts ride on FolderOut.
    listed = {f["id"]: f for f in client.get("/api/folders").json()}
    assert listed[fid]["share_people"] == 1
    assert listed[fid]["share_groups"] == 0

    # Once that person signs in, status flips to member — same share row.
    auth_as(app, "future@x")
    auth_as(app, "owner@x")
    r = client.get(f"/api/folders/{fid}/sharing")
    assert r.json()["people"][0]["status"] == "member"

    # Remove fully.
    r = client.delete(f"/api/folders/{fid}/sharing/email?email=future@x")
    assert r.status_code == 200
    assert r.json()["people"] == []


def test_legacy_grant_shows_and_removes_both_stores(
    client: TestClient, app, tmp_path: Path
) -> None:
    """Pre-feature folder_acl grants surface in the people list (legacy)
    and Remove wipes BOTH stores so no zombie access survives."""
    folder = _register(client, app, "owner@x", tmp_path, "docs")
    fid = folder["id"]
    grantee = auth_as(app, "old.grantee@x")
    auth_as(app, "owner@x")
    with session_scope() as s:
        grant_folder(s, fid, grantee)

    r = client.get(f"/api/folders/{fid}/sharing")
    people = r.json()["people"]
    assert people == [
        {
            "email": "old.grantee@x",
            "status": "member",
            "outside_org": False,
            "legacy": True,
        }
    ]
    # Legacy grants count as people on the pill too.
    listed = {f["id"]: f for f in client.get("/api/folders").json()}
    assert listed[fid]["share_people"] == 1

    r = client.delete(f"/api/folders/{fid}/sharing/email?email=old.grantee@x")
    assert r.status_code == 200
    assert r.json()["people"] == []
    with session_scope() as s:
        # The grantee's rows are gone from BOTH stores. (The owner's own
        # registration-time self-grant in folder_acl legitimately remains.)
        assert (
            s.query(FolderAcl)
            .filter_by(folder_id=fid, user_id=grantee)
            .count()
            == 0
        )
        assert s.query(FolderEmailAcl).filter_by(folder_id=fid).count() == 0


def test_group_share_lifecycle(client: TestClient, app, tmp_path: Path) -> None:
    _allow_native("owner@x")
    folder = _register(client, app, "owner@x", tmp_path, "docs")
    fid = folder["id"]
    member = auth_as(app, "member@x")
    auth_as(app, "owner@x")
    with session_scope() as s:
        g = Group(name="eng")
        s.add(g)
        s.flush()
        s.add(UserGroup(user_id=member, group_id=g.id))
        gid = g.id

    # Pick-list is visible to any signed-in user.
    r = client.get("/api/groups")
    assert r.status_code == 200
    assert r.json() == [{"id": gid, "name": "eng", "member_count": 1}]

    r = client.post(f"/api/folders/{fid}/sharing/group", json={"group_id": gid})
    assert r.status_code == 200, r.text
    assert r.json()["groups"] == [{"id": gid, "name": "eng", "member_count": 1}]

    # Member now sees the folder.
    auth_as(app, "member@x")
    ids = [f["id"] for f in client.get("/api/folders").json()]
    assert fid in ids

    # Unknown group → 404, unshare → gone.
    auth_as(app, "owner@x")
    assert (
        client.post(
            f"/api/folders/{fid}/sharing/group", json={"group_id": 999}
        ).status_code
        == 404
    )
    r = client.delete(f"/api/folders/{fid}/sharing/group/{gid}")
    assert r.json()["groups"] == []
    auth_as(app, "member@x")
    assert fid not in [f["id"] for f in client.get("/api/folders").json()]


def test_audience_variants(client: TestClient, app, tmp_path: Path) -> None:
    # Community-less owner → kind "none"; audience toggle rejected but
    # people shares still work.
    folder = _register(client, app, "loner@x", tmp_path, "solo")
    fid = folder["id"]
    r = client.get(f"/api/folders/{fid}/sharing")
    assert r.json()["audience"] == {"kind": "none", "on": False, "label": ""}
    assert (
        client.patch(f"/api/folders/{fid}/share", json={"shared": True}).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/folders/{fid}/sharing/email", json={"email": "a@b.c"}
        ).status_code
        == 200
    )

    # Native owner → kind "native_all"; toggling the audience leaves the
    # people share fully intact (layer independence via the API).
    _allow_native("nat@x")
    folder2 = _register(client, app, "nat@x", tmp_path, "nat")
    fid2 = folder2["id"]
    client.post(f"/api/folders/{fid2}/sharing/email", json={"email": "kept@x"})
    assert client.patch(
        f"/api/folders/{fid2}/share", json={"shared": True}
    ).status_code == 200
    r = client.get(f"/api/folders/{fid2}/sharing")
    body = r.json()
    assert body["audience"]["kind"] == "native_all"
    assert body["audience"]["on"] is True
    assert [p["email"] for p in body["people"]] == ["kept@x"]
    client.patch(f"/api/folders/{fid2}/share", json={"shared": False})
    r = client.get(f"/api/folders/{fid2}/sharing")
    assert r.json()["audience"]["on"] is False
    assert [p["email"] for p in r.json()["people"]] == ["kept@x"]
