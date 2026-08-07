"""Live Clerk reads on screen/action paths (no re-login, no restart).

The complaint behind these: a user promoted to org-admin (or newly added to
an org) in Clerk saw nothing change until a restart. The fix fetches Clerk
LIVE on the screen/action paths — ``/me`` re-provisions accounts, the cvk
gate re-checks the role — while the per-request cvk AUTH hot path keeps its
short TTL cache (so we don't hammer Clerk on every request).

Pinned here:
- ``/me`` provisions a newly-added org membership without re-login (L3).
- ``/me`` is fail-soft when Clerk is down (no 500, keeps existing accounts).
- ``/me`` is idempotent (no duplicate rows across calls).
- the cvk management gate reflects a role change LIVE (force_refresh bypass).
- the cvk AUTH hot path stays cached (guard against making it live).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import auth_as
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import User
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc

# One instance; sagiv starts in only "Agnitio", later gains "Wonder".
_DIR = {
    "users": [{"id": "u_s", "email": "sagiv@agnitio.ai", "name": "Sagiv",
               "orgs": [{"id": "org_agn", "name": "Agnitio"}]}],
    "organizations": [
        {"id": "org_agn", "name": "Agnitio",
         "members": [{"email": "sagiv@agnitio.ai", "role": "member"}]},
    ],
}


def _enable_clerk() -> None:
    admin_store.save_clerk_instances(
        [{"name": "Development", "secret_key": "sk_test_x", "enabled": True}]
    )
    clerk_svc.clear_org_members_cache()
    import voitta_rag_enterprise.services.admin_scope as scope_mod

    scope_mod.clear_directory_cache()


def _accounts(email: str) -> dict[str, str]:
    with session_scope() as s:
        return {
            u.company_id: u.company_name
            for u in s.execute(select(User).where(User.email == email)).scalars()
        }


def test_me_provisions_new_membership_without_relogin(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_clerk()
    directory = {"users": [dict(_DIR["users"][0])], "organizations": []}

    async def fake_directory(secret_key: str) -> dict:
        return directory

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)
    auth_as(app, "sagiv@agnitio.ai")

    with TestClient(app) as c:
        me1 = c.get("/api/auth/me").json()
        got = {a["company_name"] for a in me1["accounts"]}
        assert "Development / Agnitio" in got
        assert not any("Wonder" in n for n in got)

        # Sagiv is added to a second org in Clerk — NO re-login.
        directory["users"][0]["orgs"] = [
            {"id": "org_agn", "name": "Agnitio"},
            {"id": "org_won", "name": "Wonder"},
        ]
        me2 = c.get("/api/auth/me").json()
        got2 = {a["company_name"] for a in me2["accounts"]}
        assert "Development / Wonder" in got2  # new account appeared live

    assert _accounts("sagiv@agnitio.ai")["org_won"] == "Development / Wonder"


def test_me_fail_soft_when_clerk_down(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_clerk()
    # Pre-existing account from a past login.
    from voitta_rag_enterprise.services.acl import get_or_create_user

    with session_scope() as s:
        get_or_create_user(s, "sagiv@agnitio.ai", "org_agn", "Development / Agnitio")
        s.commit()

    async def boom(secret_key: str) -> dict:
        raise clerk_svc.ClerkError("down")

    monkeypatch.setattr(clerk_svc, "fetch_directory", boom)
    auth_as(app, "sagiv@agnitio.ai")
    with TestClient(app) as c:
        r = c.get("/api/auth/me")
        assert r.status_code == 200  # never 500
        got = {a["company_id"] for a in r.json()["accounts"]}
        assert "org_agn" in got  # existing account preserved, not dropped


def test_me_idempotent_no_duplicate_rows(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_clerk()

    async def fake_directory(secret_key: str) -> dict:
        return _DIR

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)
    auth_as(app, "sagiv@agnitio.ai")
    with TestClient(app) as c:
        for _ in range(3):
            c.get("/api/auth/me")
    with session_scope() as s:
        rows = s.execute(
            select(User).where(User.email == "sagiv@agnitio.ai")
        ).scalars().all()
    # Personal + Agnitio, exactly once each.
    assert sorted(u.company_id for u in rows) == ["", "org_agn"]


def test_cvk_gate_reflects_role_change_live(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member promoted to admin sees the cvk option on the next Settings
    open — the gate force-refreshes, bypassing the 300s membership cache."""
    _enable_clerk()
    role = {"v": "member"}

    async def fake_members(secret_key: str, org_id: str, **_kw) -> dict[str, str]:
        return {"sagiv@agnitio.ai": role["v"]}

    monkeypatch.setattr(clerk_svc, "fetch_org_members", fake_members)

    # Seed sagiv's Agnitio account and act as it (NOT person-level admin).
    from voitta_rag_enterprise.services.acl import get_or_create_user

    with session_scope() as s:
        acc = get_or_create_user(s, "sagiv@agnitio.ai", "org_agn", "Development / Agnitio")
        s.commit()
        acc_id = acc.id
    from voitta_rag_enterprise.api.deps import current_user, real_user
    from voitta_rag_enterprise.services.acl import CurrentUser

    fake = lambda: CurrentUser(  # noqa: E731
        id=acc_id, email="sagiv@agnitio.ai",
        company_id="org_agn", company_name="Development / Agnitio",
    )
    app.dependency_overrides[current_user] = fake
    app.dependency_overrides[real_user] = fake

    with TestClient(app) as c:
        # Member → no cvk management (403 → SPA hides the section).
        assert c.get("/api/auth/company-keys").status_code == 403
        # Promoted in Clerk → next open sees it LIVE, no cache wait.
        role["v"] = "admin"
        assert c.get("/api/auth/company-keys").status_code == 200


async def test_cvk_auth_hot_path_stays_cached(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-request cvk AUTH check must use the TTL cache — a second
    call within the window does NOT re-hit Clerk. Guards against
    accidentally making the hot path live (which would hammer Clerk)."""
    _enable_clerk()
    calls = {"n": 0}

    # Mock the HTTP layer BELOW the cache so the real caching in
    # fetch_org_members actually runs (mocking fetch_org_members itself
    # would bypass the very cache under test).
    async def counting_http(client, path, secret_key) -> list[dict]:
        calls["n"] += 1
        return [{"public_user_data": {"identifier": "sagiv@agnitio.ai"},
                 "role": "org:member"}]

    monkeypatch.setattr(clerk_svc, "_get_paginated", counting_http)
    from voitta_rag_enterprise.api.routes.company_keys import _email_in_scope

    assert await _email_in_scope("sagiv@agnitio.ai", "org_agn") is True
    assert await _email_in_scope("sagiv@agnitio.ai", "org_agn") is True
    assert calls["n"] == 1  # cached — not re-fetched per request
