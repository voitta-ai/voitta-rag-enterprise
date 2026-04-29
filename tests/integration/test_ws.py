"""WebSocket endpoint tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_ws_subscribe_handshake(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["files", "jobs"]})
        msg = ws.receive_json()
        assert msg == {"type": "subscribed", "topics": ["files", "jobs"]}


def test_ws_subscribe_with_no_topics_uses_all(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe"})
        msg = ws.receive_json()
        assert msg["type"] == "subscribed"
        assert set(msg["topics"]) == {"folders", "files", "jobs", "stats"}


def test_ws_first_message_must_be_subscribe(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_ws_invalid_topics_returns_error(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["nonsense"]})
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_ws_receives_folder_added_event(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["folders"]})
        ws.receive_json()  # subscribed

        r = client.post("/api/folders", json={"path": str(src)})
        assert r.status_code == 201

        event = ws.receive_json()
        assert event["type"] == "folder.added"
        assert event["folder"]["path"] == str(src)


def test_ws_receives_folder_removed_event(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    fid = client.post("/api/folders", json={"path": str(src)}).json()["id"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["folders"]})
        ws.receive_json()
        client.delete(f"/api/folders/{fid}")
        event = ws.receive_json()
        assert event == {"type": "folder.removed", "folder_id": fid}
