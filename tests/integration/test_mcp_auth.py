"""MCP bearer-auth middleware tests.

We exercise the spliced ``/mcp`` route on the unified app and the standalone
``mcp_server.build_app`` ASGI app. Both wrap the same ``BearerAuthMiddleware``,
so we cover them once each to catch wiring regressions.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from voitta_image_rag.config import reset_settings_cache


def _create_key(client: TestClient, email: str) -> str:
    """Mint a key for ``email`` via the REST API and return its plaintext token."""
    r = client.post(
        "/api/auth/keys",
        json={"name": "mcp-test"},
        headers={"X-Forwarded-Email": email},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _unified_app():
    from voitta_image_rag.main import create_app

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
    with TestClient(_unified_app()) as client:
        token = _create_key(client, "alice@x")
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
    with TestClient(_unified_app()) as client:
        token = _create_key(client, "alice@x")
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

    # Second request to confirm the token still verifies after the first
    # call's ``last_used_at`` write committed.
    with TestClient(_unified_app()) as client:
        keys = client.get(
            "/api/auth/keys", headers={"X-Forwarded-Email": "alice@x"}
        ).json()
        assert keys[0]["last_used_at"] is not None


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
    from voitta_image_rag.mcp_server import build_app

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
    authenticates via X-Forwarded-Email / session cookie."""
    with TestClient(_unified_app()) as client:
        r = client.get(
            "/api/auth/me", headers={"X-Forwarded-Email": "alice@x"}
        )
        assert r.status_code == 200, r.text
