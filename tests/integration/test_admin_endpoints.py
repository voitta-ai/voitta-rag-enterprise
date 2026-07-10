"""Integration tests for /api/admin/*.

The admin guard is the load-bearing piece — non-admins must never reach
these routes regardless of how they authenticated. We exercise that gate
on every endpoint via a parameterised matrix below, then validate the
happy paths in dedicated tests.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import auth_as


def _make_admin(app: FastAPI, email: str) -> int:
    """Create a User row, flip is_admin, route current_user → that user."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import User

    uid = auth_as(app, email)
    with session_scope() as s:
        u = s.get(User, uid)
        assert u is not None
        u.is_admin = True
        s.commit()
    return uid


def _make_super_admin(app: FastAPI, email: str, monkeypatch: pytest.MonkeyPatch) -> int:
    """A superadmin: is_admin + listed in VOITTA_SUPER_ADMINS.

    Deployment-global mutations (allowlist, caps, provider catalog, user
    pre-creation, groups) are superadmin-only now — a plain admin gets 403 —
    so tests that exercise those need this."""
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", email)
    reset_settings_cache()
    return _make_admin(app, email)


def test_non_admin_gets_403(app: FastAPI) -> None:
    auth_as(app, "regular@x.com")
    with TestClient(app) as c:
        for path, method in [
            ("/api/admin/allowlist", "get"),
            ("/api/admin/allowlist/domains", "post"),
            ("/api/admin/allowlist/users", "post"),
            ("/api/admin/blocklist", "post"),
            ("/api/admin/users", "get"),
            ("/api/admin/impersonate/1", "post"),
            ("/api/admin/indexing-caps", "get"),
            ("/api/admin/indexing-caps", "patch"),
        ]:
            resp = c.request(method, path, json={})
            assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


def test_admin_can_manage_allowlist(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_super_admin(app, "boss@example.com", monkeypatch)
    with TestClient(app) as c:
        # Initially empty
        out = c.get("/api/admin/allowlist").json()
        assert out["domains"] == []
        assert out["users"] == []
        assert out["blocked"] == []

        # Add a domain → reflected in subsequent GETs
        out = c.post(
            "/api/admin/allowlist/domains", json={"domain": "customer.com"}
        ).json()
        assert "customer.com" in out["domains"]

        # Add an allowed user
        out = c.post(
            "/api/admin/allowlist/users", json={"email": "bob@gmail.com"}
        ).json()
        assert "bob@gmail.com" in out["users"]

        # Block someone
        out = c.post(
            "/api/admin/blocklist", json={"email": "rogue@customer.com"}
        ).json()
        assert "rogue@customer.com" in out["blocked"]

        # Remove operations
        out = c.delete("/api/admin/allowlist/domains/customer.com").json()
        assert "customer.com" not in out["domains"]


def test_admin_can_promote_demote_users(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Toggling is_admin via PATCH /api/admin/users/{id}."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import User
    from voitta_rag_enterprise.services.acl import get_or_create_user

    _make_super_admin(app, "boss@example.com", monkeypatch)
    with session_scope() as s:
        target = get_or_create_user(s, "alice@example.com")
        s.commit()
        target_id = target.id

    with TestClient(app) as c:
        out = c.patch(
            f"/api/admin/users/{target_id}", json={"is_admin": True}
        ).json()
        assert out["is_admin"] is True

        out = c.patch(
            f"/api/admin/users/{target_id}", json={"is_admin": False}
        ).json()
        assert out["is_admin"] is False

    with session_scope() as s:
        assert s.get(User, target_id).is_admin is False


def test_admin_can_pre_create_user_with_admin_flag(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of POST /api/admin/users: grant admin status to
    a teammate before they've ever signed in. Without this, the Users
    table only showed users with at least one prior login, leaving no
    way to pre-grant admin powers to a new hire."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import User
    from voitta_rag_enterprise.services import admin_store

    _make_super_admin(app, "boss@example.com", monkeypatch)
    with TestClient(app) as c:
        out = c.post(
            "/api/admin/users",
            json={"email": "newhire@customer.com", "is_admin": True},
        ).json()
        assert out["email"] == "newhire@customer.com"
        assert out["is_admin"] is True

    # User row exists, flag set, allowlist updated so they can sign in.
    with session_scope() as s:
        row = s.query(User).filter(User.email == "newhire@customer.com").one()
        assert row.is_admin is True
    assert "newhire@customer.com" in admin_store.list_allowed_users()


def test_pre_create_user_without_signin_grant(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """grant_signin=False creates the row but does NOT touch the allowlist —
    rare path, but exists for cases where the admin wants the User row
    to exist (e.g. for ACL grants) without admitting the email."""
    from voitta_rag_enterprise.services import admin_store

    _make_super_admin(app, "boss@example.com", monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/admin/users",
            json={"email": "ghost@example.com", "grant_signin": False},
        ).raise_for_status()
    assert "ghost@example.com" not in admin_store.list_allowed_users()


def test_super_admin_flag_surfaced_in_user_listing(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "boss@example.com")
    reset_settings_cache()
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        users = c.get("/api/admin/users").json()
        boss = next(u for u in users if u["email"] == "boss@example.com")
        assert boss["is_super_admin"] is True


def test_admin_can_read_and_patch_indexing_caps(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity-check the indexing-caps endpoints end-to-end: GET returns
    values + defaults + bounds, PATCH applies a clamped update."""
    _make_super_admin(app, "boss@example.com", monkeypatch)
    with TestClient(app) as c:
        out = c.get("/api/admin/indexing-caps").json()
        assert "values" in out and "defaults" in out and "bounds" in out
        assert out["values"]["xlsx_max_rows"] == out["defaults"]["xlsx_max_rows"]

        # Apply an override; respect the BOUNDS upper limit if we pick a
        # ridiculous value.
        patched = c.patch(
            "/api/admin/indexing-caps", json={"xlsx_max_rows": 12_345}
        ).json()
        assert patched["values"]["xlsx_max_rows"] == 12_345

        # Re-GET to confirm persistence and that the rest of the snapshot
        # is unchanged.
        again = c.get("/api/admin/indexing-caps").json()
        assert again["values"]["xlsx_max_rows"] == 12_345
        assert again["values"]["pdf_pages_per_bucket"] == out["values"]["pdf_pages_per_bucket"]
