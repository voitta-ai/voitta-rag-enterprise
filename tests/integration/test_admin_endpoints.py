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
        ]:
            resp = c.request(method, path, json={})
            assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


def test_admin_can_manage_allowlist(app: FastAPI) -> None:
    _make_admin(app, "boss@example.com")
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


def test_admin_can_promote_demote_users(app: FastAPI) -> None:
    """Toggling is_admin via PATCH /api/admin/users/{id}."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import User
    from voitta_rag_enterprise.services.acl import get_or_create_user

    _make_admin(app, "boss@example.com")
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
