"""End-to-end admin scoping over the REAL session-cookie login flow.

test_admin_scope_endpoints.py exercises the scoping via dependency overrides
(fast, but it bypasses auth). This file drives the *actual* Google-OAuth +
Clerk callback so a genuine ``voitta_session`` cookie authenticates the
subsequent ``/api/admin/*`` calls — proving the whole chain works: cookie →
``real_user`` → ``admin_user`` → ``admin_scope`` (Clerk-role-derived domain).

This is the path the browser admin console actually uses — the one where the
original leak was observed — so it's the closest automated proxy to a human
logging in as ivan and opening Admin.

Google token/userinfo HTTP and the Clerk directory fetch are stubbed; no
network. The directory below makes ``orgadmin@acme.co`` an org admin of
org_1, ``carol@beta.co`` an admin of the unrelated org_2.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import User
from voitta_rag_enterprise.services import admin_scope as scope_mod
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc
from voitta_rag_enterprise.services.acl import get_or_create_user

# fetch_directory's real shape: users carry orgs [{id,name}] (login admission
# + account provisioning), organizations carry members [{email,role}] (the
# org-admin signal admin_scope derives the domain from).
_DIRECTORY = {
    "users": [
        {"id": "u_oa", "email": "orgadmin@acme.co", "name": "Org Admin",
         "orgs": [{"id": "org_1", "name": "Acme"}]},
        {"id": "u_al", "email": "alice@acme.co", "name": "Alice",
         "orgs": [{"id": "org_1", "name": "Acme"}]},
        {"id": "u_ca", "email": "carol@beta.co", "name": "Carol",
         "orgs": [{"id": "org_2", "name": "Beta"}]},
    ],
    "organizations": [
        {"id": "org_1", "name": "Acme", "members": [
            {"user_id": "u_oa", "email": "orgadmin@acme.co", "role": "admin"},
            {"user_id": "u_al", "email": "alice@acme.co", "role": "member"},
        ]},
        {"id": "org_2", "name": "Beta", "members": [
            {"user_id": "u_ca", "email": "carol@beta.co", "role": "admin"},
        ]},
    ],
}

_USERINFO: dict[str, object] = {}


class _StubResponse:
    def __init__(self, body: dict) -> None:
        self.status_code = 200
        self._body = body
        self.text = ""

    def json(self) -> dict:
        return self._body


class _StubAsyncClient:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, *_, **__):
        return _StubResponse({"access_token": "stub-token"})

    async def get(self, *_, **__):
        return _StubResponse(_USERINFO)


@pytest.fixture
def clerk_app(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FastAPI]:
    """App with Google OAuth + Clerk enabled and the directory/httpx stubbed."""
    from voitta_rag_enterprise.api.routes import auth as auth_mod
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.main import create_app

    monkeypatch.setenv("VOITTA_COOKIE_SECURE", "false")
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", "stub-client-id")
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", "stub-client-secret")
    monkeypatch.setenv("VOITTA_SESSION_SECRET", "x" * 64)
    monkeypatch.delenv("VOITTA_DEV_USER", raising=False)
    monkeypatch.delenv("VOITTA_SINGLE_USER", raising=False)
    reset_settings_cache()

    # Google HTTP stub (login callback).
    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", _StubAsyncClient)
    # Clerk enabled + directory stub (shared by login admission AND
    # admin_scope resolution — both call clerk_svc.fetch_directory).
    scope_mod.clear_directory_cache()
    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")

    async def fake_directory(secret_key: str) -> dict:
        return _DIRECTORY

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)
    yield create_app()


def _login(c: TestClient, email: str) -> None:
    """Drive the real OAuth callback; leaves the session cookie on ``c``."""
    global _USERINFO
    _USERINFO = {"email": email, "email_verified": True, "name": "T"}
    start = c.get("/api/auth/login/google", follow_redirects=False)
    assert start.status_code == 307
    state = start.headers["location"].split("state=", 1)[1].split("&", 1)[0]
    resp = c.get(
        f"/api/auth/google/callback?code=stub&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"login denied: {resp.text[:200]}"


def _promote(email: str) -> None:
    """Grant admin (person-level) — simulates a superadmin having promoted
    them. A Clerk org-admin role alone does NOT set User.is_admin."""
    with session_scope() as s:
        for u in s.query(User).filter(User.email == email):
            u.is_admin = True


def _seed_other_accounts() -> dict[str, int]:
    with session_scope() as s:
        alice = get_or_create_user(s, "alice@acme.co", "org_1", "Acme")
        carol = get_or_create_user(s, "carol@beta.co", "org_2", "Beta")
        s.flush()
        return {"alice_org1": alice.id, "carol_org2": carol.id}


# ---------------------------------------------------------------------------
# Regular org-admin: real cookie → scoped console
# ---------------------------------------------------------------------------


def test_session_org_admin_sees_only_their_org(clerk_app: FastAPI) -> None:
    with TestClient(clerk_app) as c:
        _login(c, "orgadmin@acme.co")   # provisions the org_1 account via Clerk
        _promote("orgadmin@acme.co")
        ids = _seed_other_accounts()

        users = c.get("/api/admin/users")
        assert users.status_code == 200, users.text
        emails = {u["email"] for u in users.json()}
        # Own org (self + member alice) visible…
        assert "orgadmin@acme.co" in emails
        assert "alice@acme.co" in emails
        # …the unrelated org_2 admin is NOT (the reported leak).
        assert "carol@beta.co" not in emails

        # Out-of-domain user is 404 (existence-hidden) on every verb.
        assert c.patch(
            f"/api/admin/users/{ids['carol_org2']}", json={"is_admin": True}
        ).status_code == 404
        assert c.delete(
            f"/api/admin/users/{ids['carol_org2']}"
        ).status_code == 404
        assert c.post(
            f"/api/admin/impersonate/{ids['carol_org2']}"
        ).status_code == 404
        # In-domain user is editable.
        assert c.patch(
            f"/api/admin/users/{ids['alice_org1']}", json={"display_name": "Al"}
        ).status_code == 200


def test_session_org_admin_global_settings_readonly(clerk_app: FastAPI) -> None:
    with TestClient(clerk_app) as c:
        _login(c, "orgadmin@acme.co")
        _promote("orgadmin@acme.co")

        # GET (view) works…
        assert c.get("/api/admin/allowlist").status_code == 200
        assert c.get("/api/admin/indexing-caps").status_code == 200
        assert c.get("/api/admin/settings").status_code == 200
        # …every global mutation is 403.
        assert c.post(
            "/api/admin/allowlist/domains", json={"domain": "x.com"}
        ).status_code == 403
        assert c.patch(
            "/api/admin/indexing-caps", json={"xlsx_max_rows": 5}
        ).status_code == 403
        assert c.patch(
            "/api/admin/settings", json={"nfs_root": "/tmp"}
        ).status_code == 403
        assert c.post(
            "/api/admin/users", json={"email": "z@x.com"}
        ).status_code == 403


def test_session_clerk_directory_scoped(clerk_app: FastAPI) -> None:
    with TestClient(clerk_app) as c:
        _login(c, "orgadmin@acme.co")
        _promote("orgadmin@acme.co")
        out = c.get("/api/admin/clerk/directory")
        assert out.status_code == 200, out.text
        body = out.json()
        assert {o["id"] for o in body["organizations"]} == {"org_1"}
        assert "carol@beta.co" not in {u["email"] for u in body["users"]}


# ---------------------------------------------------------------------------
# Superadmin: real cookie → full console
# ---------------------------------------------------------------------------


def test_session_superadmin_sees_all_and_mutates(
    clerk_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "root@super.co")
    reset_settings_cache()
    with TestClient(clerk_app) as c:
        _login(c, "root@super.co")  # super admitted natively, is_admin stamped
        ids = _seed_other_accounts()

        emails = {u["email"] for u in c.get("/api/admin/users").json()}
        assert {"alice@acme.co", "carol@beta.co"} <= emails  # both orgs

        # Global mutation succeeds for a superadmin.
        assert c.post(
            "/api/admin/allowlist/domains", json={"domain": "ok.com"}
        ).status_code == 200
        # Cross-org user edit allowed.
        assert c.patch(
            f"/api/admin/users/{ids['carol_org2']}", json={"display_name": "C"}
        ).status_code == 200


# ---------------------------------------------------------------------------
# Non-admin session: the console stays shut
# ---------------------------------------------------------------------------


def test_session_plain_user_gets_403(clerk_app: FastAPI) -> None:
    with TestClient(clerk_app) as c:
        _login(c, "alice@acme.co")  # org member, never promoted
        assert c.get("/api/admin/users").status_code == 403
        assert c.get("/api/admin/allowlist").status_code == 403
