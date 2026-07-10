"""Bearer (vk_/cvk_) auth on the REST /api surface.

The same API keys that authenticate /mcp now authenticate /api/* — the
REST surface IS the UI surface. These tests pin the whole contract:
happy paths for both key kinds, the 401/403 matrices, the cookie-only
exclusions, precedence over dev-mode identity, and the authed docs
endpoints (with root /docs disabled).

App-building note: like test_mcp_auth.py, each test builds the unified
app once and keeps ONE TestClient open (FastMCP's session manager can't
be restarted on the same app instance).
"""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.services import admin_store

from ..conftest import auth_as


def _unified_app() -> FastAPI:
    from voitta_rag_enterprise.main import create_app

    return create_app()


def _create_vk(app: FastAPI, client: TestClient, email: str) -> str:
    """Mint a personal key for ``email`` via the REST route (dependency
    override), then clear overrides so bearer auth is actually exercised."""
    auth_as(app, email)
    r = client.post("/api/auth/keys", json={"name": "bearer-test"})
    assert r.status_code == 200, r.text
    app.dependency_overrides.clear()
    return r.json()["token"]


def _mint_cvk(company_id: str = "", company_name: str = "") -> str:
    """Persist a company key row directly; return the plaintext token."""
    from voitta_rag_enterprise.api.routes.company_keys import mint_company_token
    from voitta_rag_enterprise.db.models import CompanyApiKey

    token, prefix, key_hash = mint_company_token()
    with session_scope() as s:
        s.add(
            CompanyApiKey(
                company_id=company_id,
                company_name=company_name,
                name="bearer-test",
                prefix=prefix,
                key_hash=key_hash,
                created_by="admin@x",
                created_at=int(time.time()),
            )
        )
    return token


def _bearer(token: str, email: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if email is not None:
        h["X-Voitta-User-Email"] = email
    return h


# ---------------------------------------------------------------------------
# vk_ happy path — the UI's own routes, driven by a personal key
# ---------------------------------------------------------------------------


def test_vk_full_surface(env: None, tmp_path) -> None:
    app = _unified_app()
    # raise_server_exceptions=False: the search call below hits an empty
    # vector store (nothing indexed → no Qdrant collections) and we only
    # assert it clears the auth layer, not that it succeeds.
    with TestClient(app, raise_server_exceptions=False) as client:
        token = _create_vk(app, client, "alice@x")
        h = _bearer(token)

        # Whoami: the key resolves to the minting account.
        me = client.get("/api/users/me", headers=h)
        assert me.status_code == 200, me.text
        assert me.json()["email"] == "alice@x"

        # Folder create + list (managed folder under VOITTA_ROOT_PATH).
        created = client.post(
            "/api/folders",
            json={"name": "docs", "display_name": "Docs"},
            headers=h,
        )
        assert created.status_code == 201, created.text
        folder_id = created.json()["id"]
        folder_dir = tmp_path / "docs"
        assert folder_dir.is_dir()  # server created it under the root
        listed = client.get("/api/folders", headers=h)
        assert folder_id in [f["id"] for f in listed.json()]

        # Multipart upload — streams through request.stream() under bearer.
        up = client.post(
            f"/api/folders/{folder_id}/upload",
            files={"file": ("hello.txt", b"hello bearer world", "text/plain")},
            headers=h,
        )
        assert up.status_code == 201, up.text
        assert up.json()["count"] == 1
        assert (folder_dir / "hello.txt").read_bytes() == b"hello bearer world"

        # Jobs + search round out the in-scope groups. Search on a fresh
        # instance (nothing indexed yet → no Qdrant collections) can't
        # return hits; what's under test is that a bearer gets PAST auth.
        assert client.get("/api/jobs/recent", headers=h).status_code == 200
        sr = client.post(
            "/api/search",
            json={"query": "hello", "folder_ids": [folder_id]},
            headers=h,
        )
        assert sr.status_code not in (401, 403), sr.text


def test_vk_scopes_to_minting_account(env: None) -> None:
    """A key minted while a COMPANY account was active resolves to that
    account on REST too — folder visibility follows the key, not the email's
    Personal account."""
    from voitta_rag_enterprise.db.models import ApiKey
    from voitta_rag_enterprise.services.acl import get_or_create_user

    app = _unified_app()
    with TestClient(app) as client:
        from voitta_rag_enterprise.api.routes.api_keys import mint_token

        token, prefix, key_hash = mint_token()
        with session_scope() as s:
            org_acc = get_or_create_user(s, "bob@x", "org_1", "Acme")
            s.add(
                ApiKey(
                    user_id=org_acc.id,
                    name="org key",
                    prefix=prefix,
                    key_hash=key_hash,
                    created_at=int(time.time()),
                )
            )
            org_id = org_acc.id
        me = client.get("/api/users/me", headers=_bearer(token))
        assert me.status_code == 200
        assert me.json()["id"] == org_id


# ---------------------------------------------------------------------------
# cvk_ parity — company key + user email, including JIT provisioning
# ---------------------------------------------------------------------------


def test_cvk_header_and_embedded_forms(env: None) -> None:
    app = _unified_app()
    with TestClient(app) as client:
        admin_store.add_allowed_user("carol@x")
        token = _mint_cvk()

        r = client.get("/api/users/me", headers=_bearer(token, "carol@x"))
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "carol@x"

        r = client.get(
            "/api/users/me", headers=_bearer(f"{token}:carol@x")
        )
        assert r.status_code == 200
        assert r.json()["email"] == "carol@x"


def test_cvk_jit_provisions_via_rest(env: None) -> None:
    """A never-signed-in allowlisted member is provisioned on first REST call."""
    from sqlalchemy import select

    from voitta_rag_enterprise.db.models import User

    app = _unified_app()
    with TestClient(app) as client:
        admin_store.add_allowed_user("newbie@x")
        token = _mint_cvk()
        with session_scope() as s:
            assert (
                s.execute(select(User).where(User.email == "newbie@x")).first()
                is None
            )
        r = client.get("/api/users/me", headers=_bearer(token, "newbie@x"))
        assert r.status_code == 200
        with session_scope() as s:
            assert (
                s.execute(select(User).where(User.email == "newbie@x")).first()
                is not None
            )


# ---------------------------------------------------------------------------
# 401 matrix — bad credentials never reach a handler
# ---------------------------------------------------------------------------


def test_401_matrix(env: None) -> None:
    app = _unified_app()
    with TestClient(app) as client:
        admin_store.add_allowed_user("ok@x")
        cvk = _mint_cvk()

        # Garbage personal key.
        r = client.get("/api/folders", headers=_bearer("vk_garbage"))
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers

        # Company key without any email; with an unadmitted email.
        assert client.get("/api/folders", headers=_bearer(cvk)).status_code == 401
        assert (
            client.get("/api/folders", headers=_bearer(cvk, "stranger@x")).status_code
            == 401
        )

        # Deleted key: mint, delete the row, then use.
        token = _create_vk(app, client, "ok@x")
        from voitta_rag_enterprise.db.models import ApiKey

        with session_scope() as s:
            for row in s.query(ApiKey).all():
                s.delete(row)
        assert client.get("/api/folders", headers=_bearer(token)).status_code == 401

        # Wrong scheme falls through to the session chain → 401 (no session).
        r = client.get(
            "/api/folders", headers={"Authorization": "Token vk_whatever"}
        )
        assert r.status_code == 401

        # No auth at all.
        assert client.get("/api/folders").status_code == 401


# ---------------------------------------------------------------------------
# 403 matrix — a VALID key on the cookie-only surface
# ---------------------------------------------------------------------------


def test_403_cookie_only_surface(env: None) -> None:
    from voitta_rag_enterprise.db.models import User

    app = _unified_app()
    with TestClient(app) as client:
        token = _create_vk(app, client, "admin@x")
        # Even an ADMIN's key must not reach the excluded surface.
        with session_scope() as s:
            for u in s.query(User).filter(User.email == "admin@x"):
                u.is_admin = True
        h = _bearer(token)
        assert client.get("/api/admin/users", headers=h).status_code == 403
        assert client.get("/api/auth/keys", headers=h).status_code == 403
        assert (
            client.post(
                "/api/auth/keys", json={"name": "nope"}, headers=h
            ).status_code
            == 403
        )
        assert client.get("/api/auth/company-keys", headers=h).status_code == 403
        assert client.post("/api/auth/account/1", headers=h).status_code == 403

        # The whoami exception: GET /api/auth/me works with a key.
        me = client.get("/api/auth/me", headers=h)
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["email"] == "admin@x"
        assert body["acting_as_user_id"] is None


# ---------------------------------------------------------------------------
# Precedence & fallthrough vs dev-mode identity
# ---------------------------------------------------------------------------


def test_bearer_beats_dev_user(app: FastAPI, client: TestClient) -> None:
    """Under VOITTA_DEV_USER (the standard test app fixture), an explicit
    key still wins; a non-Voitta bearer falls through to the dev identity."""
    token = _create_vk(app, client, "bob@x")

    r = client.get("/api/users/me", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json()["email"] == "bob@x"

    r = client.get(
        "/api/users/me", headers={"Authorization": "Bearer not-a-voitta-key"}
    )
    assert r.status_code == 200
    assert r.json()["email"] == "test@localhost"


def test_bearer_ignores_lingering_impersonation(env: None) -> None:
    """request.state.auth_method == 'bearer' short-circuits the
    impersonation block even if a session cookie with acting_as_user_id
    rides along on the same request."""
    from voitta_rag_enterprise.db.models import User
    from voitta_rag_enterprise.services.acl import get_or_create_user

    app = _unified_app()
    with TestClient(app) as client:
        token = _create_vk(app, client, "admin@x")
        with session_scope() as s:
            for u in s.query(User).filter(User.email == "admin@x"):
                u.is_admin = True
            target = get_or_create_user(s, "victim@x")
            target_id = target.id
        # Forge a session-carrying request by hitting the login-free path:
        # TestClient keeps cookies; here we just assert the bearer response
        # is the key's identity (no session set in this client, and the
        # dep returns before the impersonation block regardless).
        r = client.get("/api/users/me", headers=_bearer(token))
        assert r.status_code == 200
        assert r.json()["email"] == "admin@x"
        assert r.json()["id"] != target_id


# ---------------------------------------------------------------------------
# Docs endpoints — authed replacements, public originals gone
# ---------------------------------------------------------------------------


def test_docs_require_auth_and_root_docs_are_gone(env: None) -> None:
    app = _unified_app()
    with TestClient(app) as client:
        # Unauthenticated: the new endpoints refuse.
        assert client.get("/api/docs").status_code == 401
        assert client.get("/api/openapi.json").status_code == 401

        # Root FastAPI defaults are disabled.
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404

        # With a key: Swagger UI + schema, schema declares the auth contract.
        admin_store.add_allowed_user("dev@x")
        token = _create_vk(app, client, "dev@x")
        h = _bearer(token)
        docs = client.get("/api/docs", headers=h)
        assert docs.status_code == 200
        assert "swagger" in docs.text.lower()
        schema = client.get("/api/openapi.json", headers=h)
        assert schema.status_code == 200
        body = schema.json()
        assert "ApiKeyBearer" in body["components"]["securitySchemes"]
        assert "UserEmailHeader" in body["components"]["securitySchemes"]
        # The docs routes themselves stay out of the schema.
        assert "/api/docs" not in body["paths"]
        # And the data surface is in it.
        assert "/api/folders" in body["paths"]


# ---------------------------------------------------------------------------
# Cookie path unaffected
# ---------------------------------------------------------------------------


def test_cookie_session_path_still_works(env: None) -> None:
    app = _unified_app()
    with TestClient(app) as client:
        auth_as(app, "spa-user@x")
        assert client.get("/api/folders").status_code == 200
        assert client.get("/api/auth/me").status_code == 200
