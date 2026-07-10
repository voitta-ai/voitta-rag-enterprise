"""MCP bearer-auth middleware tests.

We exercise the spliced ``/mcp`` route on the unified app and the standalone
``mcp_server.build_app`` ASGI app. Both wrap the same ``BearerAuthMiddleware``,
so we cover them once each to catch wiring regressions.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voitta_rag_enterprise.config import reset_settings_cache

from ..conftest import auth_as


def _create_key(app: FastAPI, client: TestClient, email: str) -> str:
    """Mint a key for ``email`` via the REST API and return the plaintext token.

    Uses an already-open client because FastMCP's session manager refuses to
    be (re-)started — entering the TestClient context twice on the same app
    raises ``RuntimeError: StreamableHTTPSessionManager .run() can only be
    called once per instance``.
    """
    auth_as(app, email)
    r = client.post("/api/auth/keys", json={"name": "mcp-test"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _unified_app():
    from voitta_rag_enterprise.main import create_app

    return create_app()


# ---------------------------------------------------------------------------
# Spliced /mcp on the unified app
# ---------------------------------------------------------------------------


def test_mcp_rejects_missing_bearer(env: None) -> None:
    with TestClient(_unified_app()) as client:
        # Any /mcp request without auth must be 401 — we hit a path that
        # would otherwise route into the MCP handler.
        r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping"})
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers
        assert r.json()["error"] == "unauthorized"


def test_mcp_rejects_bogus_bearer(env: None) -> None:
    with TestClient(_unified_app()) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Authorization": "Bearer vk_garbage"},
        )
        assert r.status_code == 401


def test_mcp_rejects_wrong_scheme(env: None) -> None:
    app = _unified_app()
    with TestClient(app) as client:
        token = _create_key(app, client, "alice@x")
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Authorization": f"Token {token}"},  # not Bearer
        )
        assert r.status_code == 401


def test_mcp_rejects_x_user_name_alone(env: None) -> None:
    """Self-asserted X-User-Name no longer works — that was the whole point."""
    with TestClient(_unified_app()) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"X-User-Name": "alice@x"},
        )
        assert r.status_code == 401


def test_mcp_accepts_valid_bearer_and_bumps_last_used(env: None) -> None:
    app = _unified_app()
    with TestClient(app) as client:
        token = _create_key(app, client, "alice@x")
        # We don't care about the MCP body's reply here, only that auth lets
        # the request through. Ping isn't a registered method, so we expect
        # an MCP-layer error (-32601, etc.) but NOT the middleware's 401.
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {token}",
                # MCP streamable-http requires this Accept; without it we'd
                # be testing transport plumbing instead of auth.
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code != 401, r.text

        # Confirm last_used_at was bumped — read back as alice via the override.
        auth_as(app, "alice@x")
        keys = client.get("/api/auth/keys").json()
        assert keys[0]["last_used_at"] is not None


# ---------------------------------------------------------------------------
# Company keys (cvk_… + user email) through the same middleware
# ---------------------------------------------------------------------------


def _mint_company_key_row() -> str:
    """Persist a native-space company key directly; return the token."""
    import time as _time

    from voitta_rag_enterprise.api.routes.company_keys import mint_company_token
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import CompanyApiKey

    token, prefix, key_hash = mint_company_token()
    with session_scope() as s:
        s.add(
            CompanyApiKey(
                company_id="",
                name="mcp-test",
                prefix=prefix,
                key_hash=key_hash,
                created_by="admin@x",
                created_at=int(_time.time()),
            )
        )
    return token


def test_mcp_company_key_with_email_header(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    with TestClient(_unified_app()) as client:
        admin_store.add_allowed_user("alice@x")
        token = _mint_company_key_row()
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Voitta-User-Email": "alice@x",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code != 401, r.text


def test_mcp_company_key_with_embedded_email(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    with TestClient(_unified_app()) as client:
        admin_store.add_allowed_user("alice@x")
        token = _mint_company_key_row()
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {token}:alice@x",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code != 401, r.text


def test_mcp_company_key_without_email_is_401(env: None) -> None:
    with TestClient(_unified_app()) as client:
        token = _mint_company_key_row()
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code == 401


def test_mcp_company_key_unadmitted_email_is_401(env: None) -> None:
    with TestClient(_unified_app()) as client:
        token = _mint_company_key_row()
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Voitta-User-Email": "stranger@x",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Bypass modes
# ---------------------------------------------------------------------------


def test_mcp_bypass_in_single_user_mode(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local-dev shortcut: VOITTA_SINGLE_USER lets requests through unauthed."""
    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    reset_settings_cache()
    with TestClient(_unified_app()) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code != 401


def test_mcp_bypass_in_dev_user_mode(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    reset_settings_cache()
    with TestClient(_unified_app()) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Standalone MCP app
# ---------------------------------------------------------------------------


def test_standalone_mcp_app_also_requires_bearer(env: None) -> None:
    """The standalone ASGI app (built by ``mcp_server.build_app``) must enforce
    the same bearer requirement — otherwise running uvicorn against
    ``mcp_server:run`` would expose an unauth'd surface on port 8001."""
    from voitta_rag_enterprise.mcp_server import build_app

    app = build_app(transport="streamable-http", path="/")
    with TestClient(app) as client:
        r = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Non-/mcp routes are unaffected (session cookie auth still works)
# ---------------------------------------------------------------------------


def test_api_routes_are_not_intercepted_by_mcp_auth(env: None) -> None:
    """The bearer middleware only applies under /mcp; the SPA still
    authenticates via the session cookie / dev-user shortcut."""
    app = _unified_app()
    auth_as(app, "alice@x")
    with TestClient(app) as client:
        r = client.get("/api/auth/me")
        assert r.status_code == 200, r.text
