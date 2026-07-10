"""Admin-console domain scoping over /api/admin/* (the org-admin fix).

A regular admin must see and manage ONLY their administrative domain:
members of the Clerk orgs where they are an org admin (plus the native
community if native-allowed). Deployment-global settings are read-only
(superadmin-only to mutate). These tests drive the REST surface with a
regular org-admin, a native admin, and a superadmin.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import auth_as
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.services import admin_scope as scope_mod
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc
from voitta_rag_enterprise.services.acl import get_or_create_user

# orgadmin administers org_1; carol is in org_2; nate is native-only.
_DIRECTORY = {
    "users": [
        {"id": "u_oa", "email": "orgadmin@x", "orgs": [{"id": "org_1", "name": "Acme"}]},
        {"id": "u_al", "email": "alice@x", "orgs": [{"id": "org_1", "name": "Acme"}]},
        {"id": "u_ca", "email": "carol@x", "orgs": [{"id": "org_2", "name": "Beta"}]},
    ],
    "organizations": [
        {"id": "org_1", "name": "Acme", "members": [
            {"email": "orgadmin@x", "role": "admin"},
            {"email": "alice@x", "role": "member"},
        ]},
        {"id": "org_2", "name": "Beta", "members": [
            {"email": "carol@x", "role": "admin"},
        ]},
    ],
}


@pytest.fixture
def clerk_dir(monkeypatch: pytest.MonkeyPatch):
    scope_mod.clear_directory_cache()
    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")

    async def fake_directory(secret_key: str) -> dict:
        return _DIRECTORY

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)


def _seed_accounts() -> dict[str, int]:
    """Seed org + native accounts; return {label: user_id}."""
    with session_scope() as s:
        alice = get_or_create_user(s, "alice@x", "org_1", "Acme")
        carol = get_or_create_user(s, "carol@x", "org_2", "Beta")
        s.flush()
        ids = {"alice_org1": alice.id, "carol_org2": carol.id}
    admin_store.add_allowed_user("nate@x")
    with session_scope() as s:
        nate = get_or_create_user(s, "nate@x")  # native, Personal
        s.flush()
        ids["nate_native"] = nate.id
    return ids


def _make_regular_admin(app: FastAPI, email: str) -> int:
    from voitta_rag_enterprise.db.models import User

    uid = auth_as(app, email)
    with session_scope() as s:
        s.get(User, uid).is_admin = True
        s.commit()
    return uid


# ---------------------------------------------------------------------------
# Users list scoping + the reported leak's negative control
# ---------------------------------------------------------------------------


def test_org_admin_users_list_scoped(app: FastAPI, clerk_dir) -> None:
    _make_regular_admin(app, "orgadmin@x")
    ids = _seed_accounts()
    with TestClient(app) as c:
        users = c.get("/api/admin/users").json()
        emails = {u["email"] for u in users}
        # Sees their org's members…
        assert "alice@x" in emails
        # …NOT another org's users (the leak), NOT native users.
        assert "carol@x" not in emails
        assert "nate@x" not in emails
        _ = ids


def test_org_admin_cannot_mutate_out_of_domain_user(app: FastAPI, clerk_dir) -> None:
    _make_regular_admin(app, "orgadmin@x")
    ids = _seed_accounts()
    with TestClient(app) as c:
        # In-domain (alice, org_1) → 200.
        assert c.patch(
            f"/api/admin/users/{ids['alice_org1']}", json={"display_name": "Al"}
        ).status_code == 200
        # Out-of-domain (carol, org_2) → 404, existence-hidden.
        assert c.patch(
            f"/api/admin/users/{ids['carol_org2']}", json={"is_admin": True}
        ).status_code == 404
        assert c.delete(
            f"/api/admin/users/{ids['carol_org2']}"
        ).status_code == 404
        assert c.post(
            f"/api/admin/impersonate/{ids['carol_org2']}"
        ).status_code == 404
        # Impersonating an in-domain user works.
        assert c.post(
            f"/api/admin/impersonate/{ids['alice_org1']}"
        ).status_code == 200


def test_native_admin_sees_native_not_org(app: FastAPI, clerk_dir) -> None:
    # orgadmin is also native-allowed here → domain = org_1 + native.
    admin_store.add_allowed_user("orgadmin@x")
    _make_regular_admin(app, "orgadmin@x")
    ids = _seed_accounts()
    with TestClient(app) as c:
        emails = {u["email"] for u in c.get("/api/admin/users").json()}
        assert "nate@x" in emails      # native community now visible
        assert "alice@x" in emails     # own org
        assert "carol@x" not in emails  # still not the other org
        _ = ids


# ---------------------------------------------------------------------------
# Global settings: read-only for a regular admin (GET 200, mutation 403)
# ---------------------------------------------------------------------------


def test_regular_admin_global_settings_readonly(app: FastAPI, clerk_dir) -> None:
    _make_regular_admin(app, "orgadmin@x")
    with TestClient(app) as c:
        # GETs (view) still work.
        assert c.get("/api/admin/allowlist").status_code == 200
        assert c.get("/api/admin/indexing-caps").status_code == 200
        assert c.get("/api/admin/settings").status_code == 200
        assert c.get("/api/admin/groups").status_code == 200

        # Every global mutation → 403.
        assert c.post(
            "/api/admin/allowlist/domains", json={"domain": "x.com"}
        ).status_code == 403
        assert c.post(
            "/api/admin/blocklist", json={"email": "z@x.com"}
        ).status_code == 403
        assert c.patch(
            "/api/admin/indexing-caps", json={"xlsx_max_rows": 999}
        ).status_code == 403
        assert c.patch(
            "/api/admin/settings", json={"nfs_root": "/tmp"}
        ).status_code == 403
        assert c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "x", "client_secret": "y"},
        ).status_code == 403
        assert c.post(
            "/api/admin/groups", json={"name": "g"}
        ).status_code == 403
        assert c.post(
            "/api/admin/users", json={"email": "new@x.com"}
        ).status_code == 403


async def test_readonly_flag_in_state_snapshot(app: FastAPI, clerk_dir) -> None:
    """build_admin_state marks a regular admin's view read-only — this is
    the shape the WS snapshot / on-mutation rebuild ships to the client."""
    from voitta_rag_enterprise.api.routes.admin import build_admin_state
    from voitta_rag_enterprise.services.admin_scope import resolve_admin_scope

    _make_regular_admin(app, "orgadmin@x")
    _seed_accounts()
    with session_scope() as s:
        scope = await resolve_admin_scope(s, "orgadmin@x")
        state = build_admin_state(s, scope)
    assert state["read_only"] is True
    assert state["permissions"]["is_super"] is False
    assert state["permissions"]["admin_org_ids"] == ["org_1"]
    # The users list inside the snapshot is domain-scoped too.
    emails = {u["email"] for u in state["users"]}
    assert "alice@x" in emails and "carol@x" not in emails


# ---------------------------------------------------------------------------
# Clerk directory endpoint scoping
# ---------------------------------------------------------------------------


def test_clerk_directory_scoped_for_regular_admin(app: FastAPI, clerk_dir) -> None:
    _make_regular_admin(app, "orgadmin@x")
    with TestClient(app) as c:
        out = c.get("/api/admin/clerk/directory").json()
        org_ids = {o["id"] for o in out["organizations"]}
        assert org_ids == {"org_1"}  # only the org they administer
        emails = {u["email"] for u in out["users"]}
        assert "carol@x" not in emails  # org_2 member hidden


# ---------------------------------------------------------------------------
# Superadmin: sees all + mutations succeed
# ---------------------------------------------------------------------------


def test_superadmin_sees_all(
    app: FastAPI, clerk_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "root@x")
    reset_settings_cache()
    _make_regular_admin(app, "root@x")
    ids = _seed_accounts()
    with TestClient(app) as c:
        emails = {u["email"] for u in c.get("/api/admin/users").json()}
        assert {"alice@x", "carol@x", "nate@x"} <= emails
        # Global mutation succeeds for a superadmin.
        assert c.post(
            "/api/admin/allowlist/domains", json={"domain": "ok.com"}
        ).status_code == 200
        # Cross-org mutation allowed.
        assert c.patch(
            f"/api/admin/users/{ids['carol_org2']}", json={"display_name": "C"}
        ).status_code == 200
