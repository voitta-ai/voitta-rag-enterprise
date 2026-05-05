"""Tests for the ``POST /api/search`` route."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Image
from voitta_rag_enterprise.services.indexing import (
    run_embed_image,
    run_embed_text,
    run_extract,
)


def _png() -> bytes:
    img = PILImage.new("RGB", (8, 8), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_and_index(client: TestClient, folder_root: Path, files: dict[str, bytes | str]) -> int:
    folder_root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = folder_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
    r = client.post("/api/folders", json={"name": folder_root.name})
    folder_id = r.json()["id"]
    init_db()
    # Drive the pipeline manually since the test fixture disables background workers.
    with session_scope() as s:
        from voitta_rag_enterprise.db.models import File

        file_ids = [
            f.id for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars()
        ]
    for fid in file_ids:
        asyncio.run(run_extract({"file_id": fid}))
        asyncio.run(run_embed_text({"file_id": fid}))
        with session_scope() as s:
            for img in s.execute(select(Image).where(Image.file_id == fid)).scalars():
                asyncio.run(run_embed_image({"image_id": img.id}))
    return folder_id


def test_search_chunks_only(client: TestClient, tmp_path: Path) -> None:
    _seed_and_index(client, tmp_path / "src", {"doc.md": "alpha beta gamma delta"})
    r = client.post("/api/search", json={"query": "alpha beta", "modes": ["chunks"]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["chunks"]) >= 1
    assert body["images"] == []
    top = body["chunks"][0]
    assert "text" in top["payload"]
    assert top["payload"]["dense_model_version"]


def test_search_images_only(client: TestClient, tmp_path: Path) -> None:
    _seed_and_index(client, tmp_path / "src", {"logo.png": _png()})
    r = client.post(
        "/api/search", json={"query": "logo", "modes": ["images"], "limit": 5}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chunks"] == []
    assert len(body["images"]) >= 1


def test_search_both_modes(client: TestClient, tmp_path: Path) -> None:
    _seed_and_index(
        client,
        tmp_path / "src",
        {"doc.md": "alpha beta gamma\n\n" + "delta " * 100, "logo.png": _png()},
    )
    r = client.post("/api/search", json={"query": "alpha"})
    body = r.json()
    assert len(body["chunks"]) >= 1
    assert len(body["images"]) >= 1


def test_search_folder_filter_excludes_other_folder(client: TestClient, tmp_path: Path) -> None:
    fid_a = _seed_and_index(client, tmp_path / "a", {"a.md": "alpha"})
    _seed_and_index(client, tmp_path / "b", {"b.md": "alpha"})
    r = client.post(
        "/api/search",
        json={"query": "alpha", "modes": ["chunks"], "folder_ids": [fid_a]},
    )
    body = r.json()
    assert all(h["payload"]["folder_id"] == fid_a for h in body["chunks"])


def test_search_unauthenticated_returns_401(env: None) -> None:
    from fastapi.testclient import TestClient

    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app) as anon:
        r = anon.post("/api/search", json={"query": "x"})
        assert r.status_code == 401
