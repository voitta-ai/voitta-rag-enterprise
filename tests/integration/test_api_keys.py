"""API keys CRUD: create / list / delete, hash-only storage, user isolation."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_image_rag.db.database import session_scope
from voitta_image_rag.db.models import ApiKey


def _client():
    from voitta_image_rag.main import create_app

    return TestClient(create_app())


def test_create_returns_token_once_and_stores_only_hash(env: None) -> None:
    with _client() as client:
        r = client.post(
            "/api/auth/keys",
            json={"name": "macbook"},
            headers={"X-Forwarded-Email": "alice@x"},
        )
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
    with _client() as client:
        client.post(
            "/api/auth/keys",
            json={"name": "alice-key"},
            headers={"X-Forwarded-Email": "alice@x"},
        )
        client.post(
            "/api/auth/keys",
            json={"name": "bob-key"},
            headers={"X-Forwarded-Email": "bob@x"},
        )

        r = client.get("/api/auth/keys", headers={"X-Forwarded-Email": "alice@x"})
        assert r.status_code == 200
        keys = r.json()
        assert len(keys) == 1
        assert keys[0]["name"] == "alice-key"
        # Token is only ever in the create response, never in list responses.
        assert "token" not in keys[0]
        assert "key_hash" not in keys[0]


def test_delete_only_owner_can_delete(env: None) -> None:
    with _client() as client:
        created = client.post(
            "/api/auth/keys",
            json={"name": "alice-key"},
            headers={"X-Forwarded-Email": "alice@x"},
        ).json()
        key_id = created["id"]

        # Bob is not the owner — must look indistinguishable from "not found"
        # so the existence of someone else's key isn't probeable.
        r = client.delete(
            f"/api/auth/keys/{key_id}",
            headers={"X-Forwarded-Email": "bob@x"},
        )
        assert r.status_code == 404

        # Owner deletes successfully.
        r = client.delete(
            f"/api/auth/keys/{key_id}",
            headers={"X-Forwarded-Email": "alice@x"},
        )
        assert r.status_code == 204

        # Gone for good.
        listing = client.get(
            "/api/auth/keys", headers={"X-Forwarded-Email": "alice@x"}
        ).json()
        assert listing == []


def test_verify_token_round_trip_bumps_last_used_at(env: None) -> None:
    """The plaintext token from POST works as the lookup key, and a successful
    verify advances ``last_used_at`` so the UI can show usage."""
    from voitta_image_rag.api.routes.auth import verify_token

    with _client() as client:
        token = client.post(
            "/api/auth/keys",
            json={"name": "k"},
            headers={"X-Forwarded-Email": "alice@x"},
        ).json()["token"]

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
    with _client() as client:
        r = client.post(
            "/api/auth/keys",
            json={"name": ""},
            headers={"X-Forwarded-Email": "alice@x"},
        )
        assert r.status_code == 422  # Pydantic min_length
