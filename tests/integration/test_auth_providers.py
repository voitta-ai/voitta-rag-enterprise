"""``/api/admin/auth-providers`` — admin-managed OAuth credentials catalog.

Bootstrap from .env happens in the lifespan, but ``auth_providers_svc.upsert_env_provider``
is unit-tested below by calling it directly against a per-test session.

The validity check (``POST /…/check``) is exercised with a stubbed
``httpx.AsyncClient`` so the suite never touches the real Google
endpoint.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import auth_as


def _make_admin(app: FastAPI, email: str) -> int:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import User

    uid = auth_as(app, email)
    with session_scope() as s:
        u = s.get(User, uid)
        assert u is not None
        u.is_admin = True
        s.commit()
    return uid


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/admin/auth-providers", "get"),
        ("/api/admin/auth-providers", "post"),
        ("/api/admin/auth-providers/1", "patch"),
        ("/api/admin/auth-providers/1", "delete"),
        ("/api/admin/auth-providers/1/check", "post"),
    ],
)
def test_non_admin_is_blocked(app: FastAPI, path: str, method: str) -> None:
    auth_as(app, "regular@x.com")
    with TestClient(app) as c:
        resp = c.request(method, path, json={})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_list_update_delete_roundtrip(app: FastAPI) -> None:
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        assert c.get("/api/admin/auth-providers").json() == []

        # Create
        body = {
            "provider": "google",
            "label": "Production Google",
            "client_id": "1234.apps.googleusercontent.com",
            "client_secret": "GOCSPX-test-secret",
            "enabled": True,
        }
        out = c.post("/api/admin/auth-providers", json=body).json()
        assert out["provider"] == "google"
        assert out["label"] == "Production Google"
        assert out["client_id"] == body["client_id"]
        assert out["client_secret"] == body["client_secret"]
        assert out["enabled"] is True
        assert out["source"] == "user"
        pid = out["id"]

        # List
        rows = c.get("/api/admin/auth-providers").json()
        assert len(rows) == 1
        assert rows[0]["id"] == pid

        # Patch
        out = c.patch(
            f"/api/admin/auth-providers/{pid}",
            json={"label": "Renamed", "enabled": False},
        ).json()
        assert out["label"] == "Renamed"
        assert out["enabled"] is False
        # Untouched fields stay
        assert out["client_id"] == body["client_id"]

        # Delete
        resp = c.delete(f"/api/admin/auth-providers/{pid}")
        assert resp.status_code == 204
        assert c.get("/api/admin/auth-providers").json() == []


def test_two_simultaneous_google_rows_allowed(app: FastAPI) -> None:
    """No uniqueness constraint on (provider, client_id)."""
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        a = c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "AAA", "client_secret": "x"},
        ).json()
        b = c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "BBB", "client_secret": "y"},
        ).json()
        assert a["id"] != b["id"]
        ids = {r["id"] for r in c.get("/api/admin/auth-providers").json()}
        assert {a["id"], b["id"]} <= ids


def test_create_rejects_unknown_provider(app: FastAPI) -> None:
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        resp = c.post(
            "/api/admin/auth-providers",
            json={"provider": "okta", "client_id": "x"},
        )
        assert resp.status_code == 400
        assert "Unknown provider" in resp.json()["detail"]


def test_create_rejects_empty_client_id(app: FastAPI) -> None:
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        resp = c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "  "},
        )
        assert resp.status_code == 400


def test_patch_rejects_empty_client_id(app: FastAPI) -> None:
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        out = c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "AAA", "client_secret": "x"},
        ).json()
        resp = c.patch(
            f"/api/admin/auth-providers/{out['id']}",
            json={"client_id": ""},
        )
        assert resp.status_code == 400


def test_patch_404_for_missing_row(app: FastAPI) -> None:
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        resp = c.patch("/api/admin/auth-providers/9999", json={"label": "x"})
        assert resp.status_code == 404


def test_delete_404_for_missing_row(app: FastAPI) -> None:
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        resp = c.delete("/api/admin/auth-providers/9999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bootstrap (env upsert)
# ---------------------------------------------------------------------------


def test_upsert_env_provider_creates_row_when_missing(app: FastAPI) -> None:
    """Empty DB + .env values present → row inserted, source='env'."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import AuthProvider
    from voitta_rag_enterprise.services.auth_providers import upsert_env_provider

    with session_scope() as s:
        upsert_env_provider(
            s,
            provider="google",
            client_id="abc.apps.googleusercontent.com",
            client_secret="GOCSPX-from-env",
        )
    with session_scope() as s:
        rows = s.query(AuthProvider).all()
        assert len(rows) == 1
        assert rows[0].source == "env"
        assert rows[0].client_secret == "GOCSPX-from-env"


def test_upsert_env_provider_is_idempotent_and_refreshes_secret(app: FastAPI) -> None:
    """Re-upsert same client_id but rotated secret → secret updated, no new row."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import AuthProvider
    from voitta_rag_enterprise.services.auth_providers import upsert_env_provider

    with session_scope() as s:
        upsert_env_provider(
            s, provider="google",
            client_id="same-id", client_secret="GOCSPX-old",
        )
    with session_scope() as s:
        upsert_env_provider(
            s, provider="google",
            client_id="same-id", client_secret="GOCSPX-rotated",
        )
    with session_scope() as s:
        rows = s.query(AuthProvider).all()
        assert len(rows) == 1
        assert rows[0].client_secret == "GOCSPX-rotated"


def test_upsert_env_provider_is_noop_with_no_creds(app: FastAPI) -> None:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import AuthProvider
    from voitta_rag_enterprise.services.auth_providers import upsert_env_provider

    with session_scope() as s:
        result = upsert_env_provider(
            s, provider="google", client_id=None, client_secret=None,
        )
    assert result is None
    with session_scope() as s:
        assert s.query(AuthProvider).count() == 0


def test_user_created_row_is_not_clobbered_by_env_upsert(app: FastAPI) -> None:
    """A different client_id in .env means a new row, not a steamroller."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import AuthProvider
    from voitta_rag_enterprise.services.auth_providers import upsert_env_provider

    with session_scope() as s:
        s.add(AuthProvider(
            provider="google",
            label="user-created",
            client_id="user-id",
            client_secret="user-secret",
            enabled=True,
            source="user",
            created_at=1, updated_at=1,
        ))
        s.commit()

    with session_scope() as s:
        upsert_env_provider(
            s, provider="google",
            client_id="env-id", client_secret="env-secret",
        )

    with session_scope() as s:
        rows = sorted(s.query(AuthProvider).all(), key=lambda r: r.client_id)
        assert [r.client_id for r in rows] == ["env-id", "user-id"]
        sources = {r.client_id: r.source for r in rows}
        assert sources == {"env-id": "env", "user-id": "user"}


# ---------------------------------------------------------------------------
# Validity check
# ---------------------------------------------------------------------------


@pytest.fixture
def google_token_response(monkeypatch):
    """Patch ``httpx.AsyncClient`` so the validity probe never hits the network.

    The factory returns a setter; tests configure the response per-case.
    """
    state: dict = {"json": {}, "status_code": 400}

    class _MockResp:
        def __init__(self, j: dict, sc: int) -> None:
            self._j = j
            self.status_code = sc

        def json(self) -> dict:
            return self._j

    class _MockClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, *a, **k):
            return _MockResp(state["json"], state["status_code"])

    import voitta_rag_enterprise.services.auth_providers as svc

    monkeypatch.setattr(svc.httpx, "AsyncClient", _MockClient)

    def _set(j: dict, status_code: int = 400) -> None:
        state["json"] = j
        state["status_code"] = status_code

    return _set


def test_check_google_invalid_grant_means_credentials_valid(
    app: FastAPI, google_token_response,
) -> None:
    """Bogus code rejected as invalid_grant → client itself was accepted."""
    google_token_response({"error": "invalid_grant"})
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        out = c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "x", "client_secret": "y"},
        ).json()
        r = c.post(f"/api/admin/auth-providers/{out['id']}/check").json()
        assert r["ok"] is True


def test_check_google_invalid_client_means_credentials_wrong(
    app: FastAPI, google_token_response,
) -> None:
    google_token_response({
        "error": "invalid_client",
        "error_description": "Unauthorized",
    })
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        out = c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "x", "client_secret": "y"},
        ).json()
        r = c.post(f"/api/admin/auth-providers/{out['id']}/check").json()
        assert r["ok"] is False
        assert "Unauthorized" in r["message"]


def test_check_google_unknown_error_is_not_ok(
    app: FastAPI, google_token_response,
) -> None:
    google_token_response({
        "error": "invalid_request",
        "error_description": "Missing code",
    })
    _make_admin(app, "boss@example.com")
    with TestClient(app) as c:
        out = c.post(
            "/api/admin/auth-providers",
            json={"provider": "google", "client_id": "x", "client_secret": "y"},
        ).json()
        r = c.post(f"/api/admin/auth-providers/{out['id']}/check").json()
        assert r["ok"] is False
        assert "Missing code" in r["message"]


def test_check_microsoft_requires_tenant_id(app: FastAPI) -> None:
    """Microsoft check fails fast (no network) when tenant_id isn't set."""
    _make_admin(app, "boss@example.com")
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import AuthProvider

    # Insert directly — bypassing the API guard — to land a Microsoft row
    # with no tenant. The /check endpoint should reject it without
    # touching the network.
    with session_scope() as s:
        row = AuthProvider(
            provider="microsoft", label="MS",
            client_id="ms-id", client_secret="ms-secret",
            tenant_id="",
            enabled=True, source="user",
            created_at=1, updated_at=1,
        )
        s.add(row)
        s.flush()
        pid = row.id
    with TestClient(app) as c:
        r = c.post(f"/api/admin/auth-providers/{pid}/check").json()
        assert r["ok"] is False
        assert "tenant" in r["message"].lower()


def test_check_github_returns_not_implemented(app: FastAPI) -> None:
    """GitHub probe still unwired — check returns ok=False, message."""
    _make_admin(app, "boss@example.com")
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import AuthProvider

    with session_scope() as s:
        row = AuthProvider(
            provider="github", label="GH",
            client_id="gh-id", client_secret="gh-secret",
            enabled=True, source="user",
            created_at=1, updated_at=1,
        )
        s.add(row)
        s.flush()
        pid = row.id
    with TestClient(app) as c:
        r = c.post(f"/api/admin/auth-providers/{pid}/check").json()
        assert r["ok"] is False
        assert "not implemented" in r["message"].lower()
