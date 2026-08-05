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
    # Clerk enabled via the LEGACY settings shape (migrates to a named
    # "Development" instance); directory stub is shared by login admission
    # AND admin_scope resolution — both call clerk_svc.fetch_directory.
    scope_mod.clear_directory_cache()
    admin_store.save_settings(
        {"clerk_enabled": True, "clerk_secret_key": "sk_test_x"}
    )

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
        # Per-instance shape (migrated legacy key = "Development").
        inst = body["instances"][0]
        assert inst["name"] == "Development" and inst["ok"] is True
        assert {o["id"] for o in inst["organizations"]} == {"org_1"}
        assert "carol@beta.co" not in {u["email"] for u in inst["users"]}


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


# ---------------------------------------------------------------------------
# Named Clerk instances: dual-instance login union + prefixed labels +
# per-instance fail-soft in the directory proxy.
# ---------------------------------------------------------------------------

_PROD_DIRECTORY = {
    "users": [
        {"id": "pu_oa", "email": "orgadmin@acme.co", "name": "Org Admin",
         "orgs": [{"id": "org_p1", "name": "Acme"}]},
        {"id": "pu_pat", "email": "pat@prod.co", "name": "Pat",
         "orgs": [{"id": "org_p2", "name": "Gamma"}]},
    ],
    "organizations": [
        {"id": "org_p1", "name": "Acme", "members": [
            {"user_id": "pu_oa", "email": "orgadmin@acme.co", "role": "admin"},
        ]},
        {"id": "org_p2", "name": "Gamma", "members": [
            {"user_id": "pu_pat", "email": "pat@prod.co", "role": "admin"},
        ]},
    ],
}


@pytest.fixture
def two_instances(clerk_app: FastAPI, monkeypatch: pytest.MonkeyPatch):
    """clerk_app plus a second, Production instance with its own directory.

    fetch_directory dispatches on the secret key — each instance is its own
    universe, exactly like real Clerk.
    """
    scope_mod.clear_directory_cache()
    clerk_svc.clear_org_members_cache()
    admin_store.save_clerk_instances([
        {"name": "Development", "secret_key": "sk_test_x", "enabled": True},
        {"name": "Production", "secret_key": "sk_live_y", "enabled": True},
    ])

    async def fake_directory(secret_key: str) -> dict:
        if secret_key == "sk_live_y":
            return _PROD_DIRECTORY
        if secret_key == "sk_test_x":
            return _DIRECTORY
        raise clerk_svc.ClerkError("unknown key")

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)
    return clerk_app


def test_login_unions_instances_with_prefixed_labels(two_instances) -> None:
    """A user in BOTH instances gets accounts from both, labelled
    'Instance / Org'. IDs stay the raw org ids."""
    with TestClient(two_instances) as c:
        _login(c, "orgadmin@acme.co")
        me = c.get("/api/auth/me").json()
        by_company = {
            a["company_id"]: a["company_name"] for a in me["accounts"]
        }
        assert by_company["org_1"] == "Development / Acme"
        assert by_company["org_p1"] == "Production / Acme"


def test_login_via_second_instance_only(two_instances) -> None:
    """A Production-only user is admitted even though Development has never
    heard of them (union semantics)."""
    with TestClient(two_instances) as c:
        _login(c, "pat@prod.co")
        me = c.get("/api/auth/me").json()
        assert me["accounts"][0]["company_name"] == "Production / Gamma"


def test_one_instance_down_login_still_works(
    two_instances, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Development down: its orgs are skipped this login (fail closed per
    instance), Production admissions are unaffected."""
    async def flaky(secret_key: str) -> dict:
        if secret_key == "sk_live_y":
            return _PROD_DIRECTORY
        raise clerk_svc.ClerkError("dev down")

    monkeypatch.setattr(clerk_svc, "fetch_directory", flaky)
    with TestClient(two_instances) as c:
        _login(c, "pat@prod.co")  # would 303-deny if the union failed


def test_directory_proxy_per_instance_fail_soft(
    two_instances, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One instance down → its entry carries ok=False + the error while the
    healthy instance still returns data (no blank tab)."""
    async def flaky(secret_key: str) -> dict:
        if secret_key == "sk_test_x":
            return _DIRECTORY
        raise clerk_svc.ClerkError("prod exploded")

    monkeypatch.setattr(clerk_svc, "fetch_directory", flaky)
    with TestClient(two_instances) as c:
        _login(c, "orgadmin@acme.co")
        _promote("orgadmin@acme.co")
        body = c.get("/api/admin/clerk/directory").json()
        by_name = {i["name"]: i for i in body["instances"]}
        assert by_name["Development"]["ok"] is True
        assert by_name["Development"]["organizations"]
        assert by_name["Production"]["ok"] is False
        assert "prod exploded" in by_name["Production"]["error"]
        assert by_name["Production"]["live"] is True  # sk_live_ badge


def test_admin_settings_patch_validates_instances(
    clerk_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "root@super.co")
    reset_settings_cache()
    with TestClient(clerk_app) as c:
        _login(c, "root@super.co")

        def patch(instances):
            return c.patch(
                "/api/admin/settings", json={"clerk_instances": instances}
            )

        # Name required / duplicate / bad key prefix / enable-without-key.
        assert patch([{"name": "", "secret_key": "sk_test_a"}]).status_code == 400
        assert patch([
            {"name": "Prod", "secret_key": "sk_live_a"},
            {"name": "prod", "secret_key": "sk_live_b"},
        ]).status_code == 400
        assert patch([{"name": "P", "secret_key": "pk_live_x"}]).status_code == 400
        assert patch([{"name": "P", "enabled": True}]).status_code == 400

        # Valid save round-trips, with live/test derived from the prefix.
        r = patch([
            {"name": "Development", "secret_key": "sk_test_d", "enabled": True},
            {"name": "Production", "secret_key": "sk_live_p", "enabled": False},
        ])
        assert r.status_code == 200, r.text
        insts = {i["name"]: i for i in r.json()["clerk_instances"]}
        assert insts["Production"]["live"] is True
        assert insts["Development"]["live"] is False
        # Deprecated mirror fields track the Development card.
        assert r.json()["clerk_enabled"] is True
        assert r.json()["clerk_secret_key"] == "sk_test_d"
