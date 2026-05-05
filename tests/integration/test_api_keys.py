"""API keys CRUD: create / list / delete, hash-only storage, user isolation."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import ApiKey

from ..conftest import auth_as


def _app():
    from voitta_rag_enterprise.main import create_app

    return create_app()


def test_create_returns_token_once_and_stores_only_hash(env: None) -> None:
    app = _app()
    auth_as(app, "alice@x")
    with TestClient(app) as client:
        r = client.post("/api/auth/keys", json={"name": "macbook"})
    assert r.status_code == 200, r.text
    data = r.json()
    token = data["token"]
    assert token.startswith("vk_")
    assert data["name"] == "macbook"
    assert data["prefix"] == token[:10]
    assert data["last_used_at"] is None

    with session_scope() as s:
        rows = s.execute(select(ApiKey)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        # Plaintext is never persisted.
        assert row.key_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row.key_hash
        assert row.prefix == token[:10]


def test_list_filters_by_user_and_excludes_hash(env: None) -> None:
    app = _app()
    with TestClient(app) as client:
        auth_as(app, "alice@x")
        client.post("/api/auth/keys", json={"name": "alice-key"})
        auth_as(app, "bob@x")
        client.post("/api/auth/keys", json={"name": "bob-key"})

        auth_as(app, "alice@x")
        r = client.get("/api/auth/keys")
        assert r.status_code == 200
        keys = r.json()
        assert len(keys) == 1
        assert keys[0]["name"] == "alice-key"
        # Token is only ever in the create response, never in list responses.
        assert "token" not in keys[0]
        assert "key_hash" not in keys[0]


def test_delete_only_owner_can_delete(env: None) -> None:
    app = _app()
    with TestClient(app) as client:
        auth_as(app, "alice@x")
        created = client.post("/api/auth/keys", json={"name": "alice-key"}).json()
        key_id = created["id"]

        # Bob is not the owner — must look indistinguishable from "not found"
        # so the existence of someone else's key isn't probeable.
        auth_as(app, "bob@x")
        r = client.delete(f"/api/auth/keys/{key_id}")
        assert r.status_code == 404

        # Owner deletes successfully.
        auth_as(app, "alice@x")
        r = client.delete(f"/api/auth/keys/{key_id}")
        assert r.status_code == 204

        # Gone for good.
        listing = client.get("/api/auth/keys").json()
        assert listing == []


def test_verify_token_round_trip_bumps_last_used_at(env: None) -> None:
    """The plaintext token from POST works as the lookup key, and a successful
    verify advances ``last_used_at`` so the UI can show usage."""
    from voitta_rag_enterprise.api.routes.auth import verify_token

    app = _app()
    auth_as(app, "alice@x")
    with TestClient(app) as client:
        token = client.post("/api/auth/keys", json={"name": "k"}).json()["token"]

    with session_scope() as s:
        row = verify_token(s, token)
        assert row is not None
        assert row.name == "k"
        assert row.last_used_at is not None
        s.commit()

        # Wrong / unknown token → None, no row mutated.
        assert verify_token(s, "vk_garbage") is None
        assert verify_token(s, "") is None


def test_create_validates_name(env: None) -> None:
    app = _app()
    auth_as(app, "alice@x")
    with TestClient(app) as client:
        r = client.post("/api/auth/keys", json={"name": ""})
        assert r.status_code == 422  # Pydantic min_length


def test_unauthenticated_request_is_401(env: None) -> None:
    """No session, no env shortcut → 401 from /api/auth/keys."""
    app = _app()  # no auth_as override
    with TestClient(app) as client:
        r = client.get("/api/auth/keys")
        assert r.status_code == 401
