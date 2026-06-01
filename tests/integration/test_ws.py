"""WebSocket endpoint tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _drain_snapshot(ws) -> dict[str, list]:
    """Consume the snapshot frames up to and including ``synced``.

    Returns ``{topic: items}`` for every snapshot frame seen. Every connection
    now begins with a full state snapshot (one frame per subscribed topic) then
    a ``synced`` sentinel, before any deltas — tests call this right after the
    ``subscribed`` ack to get to the live-delta phase.
    """
    snapshots: dict[str, list] = {}
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "synced":
            return snapshots
        if frame.get("type") == "snapshot":
            snapshots[frame["topic"]] = frame["items"]


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
        assert set(msg["topics"]) == {
            "folders", "files", "jobs", "stats", "admin", "keys"
        }


def test_ws_keys_snapshot_delivered_admin_withheld(client: TestClient) -> None:
    """A non-admin connection gets its own (empty) keys snapshot but never an
    admin snapshot — the admin plane is admin-only."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["admin", "keys"]})
        assert ws.receive_json()["type"] == "subscribed"
        seen = []
        while True:
            frame = ws.receive_json()
            if frame.get("type") == "synced":
                break
            seen.append(frame["type"])
        assert "keys.snapshot" in seen
        assert "admin.snapshot" not in seen  # dev user isn't an admin


def test_ws_key_create_pushes_keys_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["keys"]})
        ws.receive_json()  # subscribed
        while ws.receive_json().get("type") != "synced":
            pass  # drain connect snapshot (keys.snapshot + synced)
        r = client.post("/api/auth/keys", json={"name": "ci-key"})
        assert r.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "keys.snapshot"
        assert [k["name"] for k in event["items"]] == ["ci-key"]


def test_ws_sends_snapshot_then_synced(client: TestClient) -> None:
    """After the ack, the server sends a snapshot per topic then ``synced``."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["folders", "files", "jobs"]})
        assert ws.receive_json()["type"] == "subscribed"
        snaps = _drain_snapshot(ws)
        # folders topic also emits an ``active`` snapshot frame.
        assert set(snaps) == {"folders", "active", "files", "jobs"}
        assert snaps["folders"] == []  # fresh install, no folders yet


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


def test_ws_snapshot_reflects_existing_folder(client: TestClient, tmp_path: Path) -> None:
    """A folder created *before* connect shows up in the snapshot — this is the
    reconnect-resync guarantee in miniature (no page reload needed)."""
    src = tmp_path / "src"
    src.mkdir()
    fid = client.post("/api/folders", json={"name": src.name}).json()["id"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["folders"]})
        assert ws.receive_json()["type"] == "subscribed"
        snaps = _drain_snapshot(ws)
        assert [f["id"] for f in snaps["folders"]] == [fid]


def test_ws_receives_folder_added_event(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["folders"]})
        ws.receive_json()  # subscribed
        _drain_snapshot(ws)  # snapshot + synced

        r = client.post("/api/folders", json={"name": src.name})
        assert r.status_code == 201

        event = ws.receive_json()
        assert event["type"] == "folder.added"
        assert event["folder"]["path"] == str(src)


def test_ws_receives_folder_removed_event(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    fid = client.post("/api/folders", json={"name": src.name}).json()["id"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "topics": ["folders"]})
        ws.receive_json()
        _drain_snapshot(ws)  # snapshot + synced
        client.delete(f"/api/folders/{fid}")
        event = ws.receive_json()
        assert event == {"type": "folder.removed", "folder_id": fid}
