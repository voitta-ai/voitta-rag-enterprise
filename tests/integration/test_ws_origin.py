"""Cross-site WebSocket hijacking guard (api/origin.py) on ``/ws``.

A browser handshake from a foreign page carries that page's ``Origin``; it
must be refused before the socket is accepted. Same-origin handshakes, the
configured public base URL / extra origins, and non-browser clients (no
``Origin`` header) are allowed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voitta_rag_enterprise.api.origin import WS_CLOSE_FORBIDDEN_ORIGIN


def _subscribe(client: TestClient, headers: dict[str, str]) -> dict:
    with client.websocket_connect("/ws", headers=headers) as ws:
        ws.send_json({"type": "subscribe", "topics": ["jobs"]})
        return ws.receive_json()


def test_foreign_origin_is_refused(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        _subscribe(client, {"origin": "https://evil.example.com"})
    assert exc.value.code == WS_CLOSE_FORBIDDEN_ORIGIN


def test_assistant_socket_refuses_foreign_origin(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
        "/ws/assistant", headers={"origin": "https://evil.example.com"}
    ) as ws:
        ws.receive_json()
    assert exc.value.code == WS_CLOSE_FORBIDDEN_ORIGIN
    with client.websocket_connect("/ws/assistant", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_same_origin_is_accepted(client: TestClient) -> None:
    # TestClient sends ``Host: testserver``.
    assert _subscribe(client, {"origin": "http://testserver"})["type"] == "subscribed"


def test_no_origin_is_accepted(client: TestClient) -> None:
    assert _subscribe(client, {})["type"] == "subscribed"


def test_configured_origins_are_accepted(
    auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.main import create_app

    monkeypatch.setenv("VOITTA_PUBLIC_BASE_URL", "https://rag.example.com/")
    monkeypatch.setenv("VOITTA_WS_ALLOWED_ORIGINS", "https://ui.example.com")
    reset_settings_cache()
    app: FastAPI = create_app()
    with TestClient(app) as client:
        for origin in ("https://rag.example.com", "https://UI.example.com/"):
            assert _subscribe(client, {"origin": origin})["type"] == "subscribed"
        with pytest.raises(WebSocketDisconnect):
            _subscribe(client, {"origin": "https://other.example.com"})
