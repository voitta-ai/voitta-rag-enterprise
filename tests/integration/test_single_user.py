"""Single-user mode short-circuits ACL filtering everywhere."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import File, Folder
from voitta_image_rag.services.indexing import run_embed_text, run_extract


@pytest.fixture
def single_user(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_image_rag.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    reset_settings_cache()


def test_listing_returns_all_folders_regardless_of_grants(
    single_user: None, tmp_path: Path
) -> None:
    """A folder created by user A is visible to user B in single-user mode."""
    from voitta_image_rag.main import create_app

    app = create_app()
    src = tmp_path / "shared"
    src.mkdir()
    with TestClient(app) as c:
        c.post("/api/folders", json={"name": src.name})
        rows = c.get("/api/folders").json()
        assert len(rows) == 1


def test_search_returns_legacy_acl_stamped_data(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Data indexed under user A is searchable after switching to single-user mode."""
    from voitta_image_rag.config import reset_settings_cache

    # First pass: multi-user. User "alice" registers a folder + indexes it.
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    reset_settings_cache()

    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.md").write_text("hidden treasure here")
    init_db()
    with session_scope() as s:
        folder = Folder(path=str(src), display_name="src")
        s.add(folder)
        s.flush()

        from voitta_image_rag.services.acl import get_or_create_user, grant_folder

        alice = get_or_create_user(s, "alice@example.com")
        grant_folder(s, folder.id, alice.id)
        s.flush()

        f = File(folder_id=folder.id, rel_path="doc.md", state="pending", last_seen_at=0)
        s.add(f)
        s.flush()
        file_id = f.id

    asyncio.run(run_extract({"file_id": file_id}))
    asyncio.run(run_embed_text({"file_id": file_id}))

    # Second pass: switch to single-user. ``root@localhost`` has no grants but
    # the search filter is bypassed, so the indexed chunks remain reachable.
    monkeypatch.delenv("VOITTA_DEV_USER")
    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    reset_settings_cache()

    from voitta_image_rag.main import create_app

    with TestClient(create_app()) as c:
        r = c.post(
            "/api/search",
            json={"query": "hidden treasure", "modes": ["chunks"]},
        )
        body = r.json()
    assert len(body["chunks"]) >= 1


def test_user_can_see_folder_helper(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_image_rag.config import reset_settings_cache
    from voitta_image_rag.services.acl import user_can_see_folder

    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    reset_settings_cache()

    init_db()
    with session_scope() as s:
        folder = Folder(path="/somewhere", display_name="x")
        s.add(folder)
        s.flush()
        # No grant exists; in single-user mode the check still returns True.
        assert user_can_see_folder(s, folder.id, user_id=999) is True
        # Unknown folder still returns False.
        assert user_can_see_folder(s, 9999, user_id=999) is False
