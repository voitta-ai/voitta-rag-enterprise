"""``/api/admin/groups`` + extended user routes (delete, patch w/ groups)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import auth_as


@pytest.fixture(autouse=True)
def _admin_is_super(auth_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Group CRUD and cross-user management are superadmin-scoped now; the
    admin these tests act as (``admin@x.com``) must be a superadmin. Regular-
    admin denial of these routes is covered in test_admin_scope_endpoints.

    Depends on ``auth_env`` so it runs AFTER that fixture strips ``VOITTA_*``
    — otherwise the env reset would wipe the super-admin var we just set."""
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "admin@x.com")
    reset_settings_cache()


def _make_admin(app: FastAPI, email: str) -> int:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import User

    uid = auth_as(app, email)
    with session_scope() as s:
        u = s.get(User, uid)
        u.is_admin = True
        s.commit()
    return uid


def _mk_user(email: str) -> int:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.services.acl import get_or_create_user

    with session_scope() as s:
        u = get_or_create_user(s, email)
        s.commit()
        return u.id


# --------------------------------------------------------------------------
# Admin gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/admin/groups", "get"),
        ("/api/admin/groups", "post"),
        ("/api/admin/groups/1", "patch"),
        ("/api/admin/groups/1", "delete"),
        ("/api/admin/groups/1/members", "post"),
        ("/api/admin/groups/1/members/1", "delete"),
        ("/api/admin/users/1", "delete"),
    ],
)
def test_non_admin_blocked(app: FastAPI, path: str, method: str) -> None:
    auth_as(app, "regular@x.com")
    with TestClient(app) as c:
        assert c.request(method, path, json={}).status_code == 403


# --------------------------------------------------------------------------
# Group CRUD
# --------------------------------------------------------------------------

def test_group_crud_and_dup_guard(app: FastAPI) -> None:
    _make_admin(app, "admin@x.com")
    with TestClient(app) as c:
        r = c.post("/api/admin/groups", json={"name": "eng", "description": "Engineering"})
        assert r.status_code == 200
        gid = r.json()["id"]
        assert r.json()["member_count"] == 0

        # dup name → 409
        assert c.post("/api/admin/groups", json={"name": "eng"}).status_code == 409

        # rename
        r = c.patch(f"/api/admin/groups/{gid}", json={"name": "engineering"})
        assert r.status_code == 200 and r.json()["name"] == "engineering"

        assert [g["name"] for g in c.get("/api/admin/groups").json()] == ["engineering"]

        assert c.delete(f"/api/admin/groups/{gid}").status_code == 204
        assert c.get("/api/admin/groups").json() == []


def test_group_membership(app: FastAPI) -> None:
    _make_admin(app, "admin@x.com")
    uid = _mk_user("member@x.com")
    with TestClient(app) as c:
        gid = c.post("/api/admin/groups", json={"name": "sales"}).json()["id"]
        assert c.post(f"/api/admin/groups/{gid}/members", json={"user_id": uid}).status_code == 204
        assert next(g for g in c.get("/api/admin/groups").json() if g["id"] == gid)["member_count"] == 1
        # the user now lists the group
        users = c.get("/api/admin/users").json()
        assert "sales" in next(u for u in users if u["id"] == uid)["groups"]
        assert c.delete(f"/api/admin/groups/{gid}/members/{uid}").status_code == 204
        assert next(g for g in c.get("/api/admin/groups").json() if g["id"] == gid)["member_count"] == 0


# --------------------------------------------------------------------------
# User patch (groups + name) and delete
# --------------------------------------------------------------------------

def test_patch_user_sets_name_and_groups_creating_on_the_fly(app: FastAPI) -> None:
    _make_admin(app, "admin@x.com")
    uid = _mk_user("alice@x.com")
    with TestClient(app) as c:
        r = c.patch(
            f"/api/admin/users/{uid}",
            json={"display_name": "Alice", "groups": ["eng", "leads"]},
        )
        assert r.status_code == 200
        assert r.json()["display_name"] == "Alice"
        assert sorted(r.json()["groups"]) == ["eng", "leads"]
        # groups were created on the fly
        assert {g["name"] for g in c.get("/api/admin/groups").json()} == {"eng", "leads"}
        # replace semantics
        r = c.patch(f"/api/admin/users/{uid}", json={"groups": ["eng"]})
        assert r.json()["groups"] == ["eng"]


def test_delete_user(app: FastAPI) -> None:
    _make_admin(app, "admin@x.com")
    uid = _mk_user("victim@x.com")
    with TestClient(app) as c:
        assert c.delete(f"/api/admin/users/{uid}").status_code == 204
        assert uid not in [u["id"] for u in c.get("/api/admin/users").json()]


def test_cannot_delete_self(app: FastAPI) -> None:
    me = _make_admin(app, "admin@x.com")
    with TestClient(app) as c:
        assert c.delete(f"/api/admin/users/{me}").status_code == 400


def test_cannot_delete_super_admin(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    # admin@x.com must stay super (to reach the delete), boss is the target super.
    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "admin@x.com,boss@x.com")
    reset_settings_cache()
    _make_admin(app, "admin@x.com")
    boss = _mk_user("boss@x.com")
    with TestClient(app) as c:
        assert c.delete(f"/api/admin/users/{boss}").status_code == 400
