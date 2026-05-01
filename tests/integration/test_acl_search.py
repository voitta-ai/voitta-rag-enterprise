"""End-to-end ACL: two users, two folders, search isolation."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy import select

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import File, FolderAcl, Image, User
from voitta_image_rag.services.indexing import (
    run_embed_image,
    run_embed_text,
    run_extract,
)

from ..conftest import auth_as


def _png(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    img = PILImage.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _drive_pipeline(folder_id: int) -> None:
    with session_scope() as s:
        ids = [
            f.id for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars()
        ]
    for fid in ids:
        asyncio.run(run_extract({"file_id": fid}))
        asyncio.run(run_embed_text({"file_id": fid}))
        with session_scope() as s:
            for img in s.execute(select(Image).where(Image.file_id == fid)).scalars():
                asyncio.run(run_embed_image({"image_id": img.id}))


def _seed_authed(
    app, client: TestClient, root: Path, layout: dict[str, str | bytes], email: str
) -> int:
    """Register a folder as ``email`` and run the indexing pipeline.

    Switches the app's current_user override to ``email`` first so the POST
    is attributed to the right user.
    """
    auth_as(app, email)
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
    r = client.post("/api/folders", json={"path": str(root)})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    init_db()
    _drive_pipeline(fid)
    return fid


def test_register_grants_creator(env: None, tmp_path: Path) -> None:
    """When alice POSTs /api/folders, alice gets the grant. Bob does not."""
    from voitta_image_rag.main import create_app

    app = create_app()
    auth_as(app, "alice@x")
    with TestClient(app) as client:
        src = tmp_path / "src"
        src.mkdir()
        client.post("/api/folders", json={"path": str(src)})

    with session_scope() as s:
        alice = s.execute(select(User).where(User.email == "alice@x")).scalar_one()
        rows = s.execute(select(FolderAcl)).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == alice.id


def test_users_only_see_their_folders_in_listing(env: None, tmp_path: Path) -> None:
    from voitta_image_rag.main import create_app

    app = create_app()
    with TestClient(app) as client:
        a = _seed_authed(app, client, tmp_path / "alice", {"a.md": "alpha"}, "alice@x")
        b = _seed_authed(app, client, tmp_path / "bob", {"b.md": "beta"}, "bob@x")

        auth_as(app, "alice@x")
        alice_list = client.get("/api/folders").json()
        auth_as(app, "bob@x")
        bob_list = client.get("/api/folders").json()

    assert {f["id"] for f in alice_list} == {a}
    assert {f["id"] for f in bob_list} == {b}


def test_listing_files_in_other_users_folder_returns_404(env: None, tmp_path: Path) -> None:
    from voitta_image_rag.main import create_app

    app = create_app()
    with TestClient(app) as client:
        b = _seed_authed(app, client, tmp_path / "bob", {"b.md": "beta"}, "bob@x")
        auth_as(app, "alice@x")
        r = client.get(f"/api/folders/{b}/files")
        assert r.status_code == 404


def test_search_chunks_filtered_by_acl(env: None, tmp_path: Path) -> None:
    from voitta_image_rag.main import create_app

    app = create_app()
    with TestClient(app) as client:
        _seed_authed(app, client, tmp_path / "alice", {"a.md": "alpha unique alphazoo"}, "alice@x")
        _seed_authed(app, client, tmp_path / "bob", {"b.md": "alpha unique alphazoo"}, "bob@x")

        # Both files contain the same text. Alice should only see her hit.
        auth_as(app, "alice@x")
        alice = client.post(
            "/api/search",
            json={"query": "alpha unique alphazoo", "modes": ["chunks"]},
        ).json()
        auth_as(app, "bob@x")
        bob = client.post(
            "/api/search",
            json={"query": "alpha unique alphazoo", "modes": ["chunks"]},
        ).json()

    assert len(alice["chunks"]) >= 1
    assert all(h["payload"]["file_path"] == "a.md" for h in alice["chunks"])
    assert len(bob["chunks"]) >= 1
    assert all(h["payload"]["file_path"] == "b.md" for h in bob["chunks"])


def test_search_images_filtered_by_acl(env: None, tmp_path: Path) -> None:
    from voitta_image_rag.main import create_app

    app = create_app()
    with TestClient(app) as client:
        _seed_authed(app, client, tmp_path / "alice", {"logo.png": _png((10, 20, 30))}, "alice@x")
        # Bob's folder has a *different* image so dedup-by-cas doesn't merge points.
        _seed_authed(app, client, tmp_path / "bob", {"logo.png": _png((200, 100, 50))}, "bob@x")

        auth_as(app, "alice@x")
        alice = client.post(
            "/api/search",
            json={"query": "any", "modes": ["images"]},
        ).json()
        auth_as(app, "bob@x")
        bob = client.post(
            "/api/search",
            json={"query": "any", "modes": ["images"]},
        ).json()

    assert all(h["payload"]["file_path"] == "logo.png" for h in alice["images"])
    assert all(h["payload"]["file_path"] == "logo.png" for h in bob["images"])
    # Distinct sets of point ids — alice should not see bob's image.
    alice_ids = {h["id"] for h in alice["images"]}
    bob_ids = {h["id"] for h in bob["images"]}
    assert alice_ids and bob_ids
    assert alice_ids.isdisjoint(bob_ids)


def test_grant_then_revoke_via_api(env: None, tmp_path: Path) -> None:
    from voitta_image_rag.main import create_app

    app = create_app()
    alice_email = "alice@example.com"
    bob_email = "bob@example.com"
    with TestClient(app) as client:
        auth_as(app, alice_email)
        client.post("/api/users", json={"email": bob_email})
        users = client.get("/api/users").json()
        bob_id = next(u["id"] for u in users if u["email"] == bob_email)

        a = _seed_authed(app, client, tmp_path / "alice", {"a.md": "alpha"}, alice_email)

        auth_as(app, bob_email)
        r = client.get(f"/api/folders/{a}/files")
        assert r.status_code == 404

        auth_as(app, alice_email)
        r = client.post(f"/api/folders/{a}/grant", json={"user_id": bob_id})
        assert r.status_code == 204

        auth_as(app, bob_email)
        r = client.get(f"/api/folders/{a}/files")
        assert r.status_code == 200

        auth_as(app, alice_email)
        r = client.post(f"/api/folders/{a}/revoke", json={"user_id": bob_id})
        assert r.status_code == 204

        auth_as(app, bob_email)
        r = client.get(f"/api/folders/{a}/files")
        assert r.status_code == 404


def test_users_me_returns_caller(env: None) -> None:
    from voitta_image_rag.main import create_app

    app = create_app()
    auth_as(app, "alice@x")
    with TestClient(app) as client:
        r = client.get("/api/users/me")
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "alice@x"


def test_grant_existing_qdrant_payload_carries_allowed_users(env: None, tmp_path: Path) -> None:
    """Stamp test: every chunk point's payload should include the granting user id."""
    from voitta_image_rag.main import create_app
    from voitta_image_rag.services import vector_store

    app = create_app()
    with TestClient(app) as client:
        _seed_authed(app, client, tmp_path / "src", {"a.md": "alpha beta"}, "alice@x")
        client_q = vector_store.get_client()
        res, _ = client_q.scroll(
            vector_store.CHUNKS, limit=100, with_payload=True
        )
    assert len(res) >= 1
    with session_scope() as s:
        alice = s.execute(select(User).where(User.email == "alice@x")).scalar_one()
        alice_id = alice.id
    for p in res:
        assert alice_id in (p.payload.get("allowed_users") or [])
