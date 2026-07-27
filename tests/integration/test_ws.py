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
        # Boot truth for the tree pill: every folder row carries sync_status.
        assert snaps["folders"][0]["sync_status"] == "idle"


def test_empty_folder_is_private_to_owner(client: TestClient, tmp_path: Path) -> None:
    """An (empty) folder one user creates must not be visible to anyone else —
    not in the ACL set, not in another user's WS snapshot, and not even to an
    admin (admins are not folder-superusers; only the admin *console* is gated
    by is_admin). Regression for folders leaking into other users' trees."""
    from voitta_rag_enterprise.api.snapshot import build_snapshot
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import Folder
    from voitta_rag_enterprise.services.acl import (
        get_or_create_user,
        visible_folder_ids,
    )

    with session_scope() as s:
        alice = get_or_create_user(s, "alice@x.com")
        bob = get_or_create_user(s, "bob@x.com")
        s.flush()
        folder = Folder(
            path=str(tmp_path / "alice-empty"),
            display_name="alice-empty",
            source_type="filesystem",
            owner_id=alice.id,
        )
        s.add(folder)
        s.flush()
        folder_id, bob_id = folder.id, bob.id

        # Data layer: Bob can't see Alice's folder.
        bob_visible = set(visible_folder_ids(s, bob_id))
        assert folder_id not in bob_visible
        assert folder_id in set(visible_folder_ids(s, alice.id))

        # Bob's WS snapshot excludes it...
        bob_frames = build_snapshot(
            s, user_id=bob_id, visible=bob_visible, is_admin=False,
            topics=("folders",),
        )
        bob_folders = next(f for f in bob_frames if f.get("topic") == "folders")
        assert folder_id not in [x["id"] for x in bob_folders["items"]]

        # ...and so does an admin Bob (is_admin doesn't widen folder visibility).
        admin_frames = build_snapshot(
            s, user_id=bob_id, visible=bob_visible, is_admin=True,
            topics=("folders",),
        )
        admin_folders = next(f for f in admin_frames if f.get("topic") == "folders")
        assert folder_id not in [x["id"] for x in admin_folders["items"]]


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


def test_files_snapshot_survives_more_files_than_sqlite_variable_limit(
    client: TestClient, tmp_path: Path
) -> None:
    """Regression: the image-count query bound one SQL variable per FILE id,
    so past SQLite's 32,766-parameter cap (~32k indexed files) every WS
    snapshot raised OperationalError('too many SQL variables') and the
    connection died on connect. Counts must be scoped by folder join, never
    by a per-file IN list."""
    import time as _time

    from sqlalchemy import text as _text

    from voitta_rag_enterprise.api.snapshot import _files_snapshot
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import Image

    src = tmp_path / "src"
    src.mkdir()
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]

    n_files = 33_000  # > 32,766
    now = int(_time.time())
    with session_scope() as s:
        # Raw executemany — ORM inserts of 33k rows are needlessly slow.
        s.execute(
            _text(
                "INSERT INTO files (folder_id, rel_path, added_at, last_seen_at,"
                " state, pending_embeds, embed_round)"
                " VALUES (:fid, :rel, :now, :now, 'indexed', 0, 0)"
            ),
            [
                {"fid": folder_id, "rel": f"bulk/f{i:05d}.txt", "now": now}
                for i in range(n_files)
            ],
        )
        first_id = s.execute(
            _text("SELECT MIN(id) FROM files WHERE folder_id = :fid"),
            {"fid": folder_id},
        ).scalar_one()
        s.add(
            Image(
                file_id=first_id, image_index=0, image_cas_id="cafe" * 16
            )
        )

    with session_scope() as s:
        # Both scopes must survive: folder-filtered and see-everything.
        items = _files_snapshot(s, {folder_id})
        assert len(items) == n_files
        by_id = {i["id"]: i for i in items}
        assert by_id[first_id]["image_count"] == 1
        assert _files_snapshot(s, None)  # single-user path, no filter

    # The reindex pre-flip binds the same id list — must also be batched.
    r = client.post(f"/api/folders/{folder_id}/reindex", json={})
    assert r.status_code == 200, r.text
    assert r.json()["scheduled"] == n_files
