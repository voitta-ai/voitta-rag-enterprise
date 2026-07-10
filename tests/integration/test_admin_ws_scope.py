"""Admin snapshot delivery over the WebSocket is per-viewer scoped.

The admin console is WS-backed: a scoped ``admin.snapshot`` on connect, and a
fresh one pushed after every admin mutation (the pump rebuilds per connection
on ``admin.invalidated``). These tests prove each admin connection gets a
snapshot scoped to *its own* domain — the old single shared broadcast (which
leaked every org to every admin) is gone.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import User
from voitta_rag_enterprise.services.acl import get_or_create_user


def _dev_is_admin() -> None:
    """Flip is_admin on the dev user (``test@localhost``) that WS auth uses."""
    with session_scope() as s:
        u = get_or_create_user(s, "test@localhost")
        u.is_admin = True


def _seed(email: str, company_id: str = "", name: str = "") -> None:
    with session_scope() as s:
        get_or_create_user(s, email, company_id, name)


def _capture_admin_snapshot(ws) -> dict | None:
    """Drain connect frames up to ``synced``; return the admin.snapshot state."""
    state = None
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "synced":
            return state
        if frame.get("type") == "admin.snapshot":
            state = frame["state"]


def test_ws_admin_snapshot_scoped_for_regular_admin(
    app: FastAPI, client: TestClient
) -> None:
    """Dev user as a regular admin with NO domain (Clerk off, not native):
    read-only snapshot, and no other-org users leak in."""
    _dev_is_admin()
    _seed("carol@x", "org_2", "Beta")
    _seed("alice@x", "org_1", "Acme")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["admin"]})
        assert ws.receive_json()["type"] == "subscribed"
        state = _capture_admin_snapshot(ws)
    assert state is not None, "admin got no admin.snapshot"
    assert state["read_only"] is True
    assert state["permissions"]["is_super"] is False
    emails = {u["email"] for u in state["users"]}
    assert "carol@x" not in emails and "alice@x" not in emails


def test_ws_admin_snapshot_full_for_super(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "test@localhost")
    reset_settings_cache()
    _dev_is_admin()
    _seed("carol@x", "org_2", "Beta")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["admin"]})
        assert ws.receive_json()["type"] == "subscribed"
        state = _capture_admin_snapshot(ws)
    assert state is not None
    assert state["read_only"] is False
    assert "carol@x" in {u["email"] for u in state["users"]}


def test_ws_admin_mutation_pushes_scoped_snapshot(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A global mutation (super-only) makes the pump push a fresh, scoped
    admin.snapshot — proving admin.invalidated triggers the per-connection
    rebuild rather than a shared broadcast payload."""
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "test@localhost")
    reset_settings_cache()
    _dev_is_admin()
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["admin"]})
        ws.receive_json()  # subscribed
        _capture_admin_snapshot(ws)  # drain the connect snapshot
        r = client.post("/api/admin/allowlist/domains", json={"domain": "acme.com"})
        assert r.status_code == 200
        frame = ws.receive_json()
        assert frame["type"] == "admin.snapshot"
        assert "acme.com" in frame["state"]["allowlist"]["domains"]
        # It is NOT the raw 'admin.invalidated' signal — that's transformed.
        assert frame["type"] != "admin.invalidated"
    # sanity: dev user row still admin
    with session_scope() as s:
        assert s.query(User).filter(User.email == "test@localhost").one().is_admin