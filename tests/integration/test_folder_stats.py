"""Tests for /api/folders/{id}/stats."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy import select

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import File, Image
from voitta_image_rag.services.indexing import (
    run_embed_image,
    run_embed_text,
    run_extract,
)


def _png(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    img = PILImage.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed(client: TestClient, root: Path, layout: dict[str, bytes | str]) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
    folder_id = client.post("/api/folders", json={"path": str(root)}).json()["id"]
    init_db()
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
    return folder_id


def test_stats_basic_counts(client: TestClient, tmp_path: Path) -> None:
    fid = _seed(
        client,
        tmp_path / "src",
        {"a.md": "hello world", "b.md": "another", "c.png": _png()},
    )
    s = client.get(f"/api/folders/{fid}/stats").json()
    assert s["files_total"] == 3
    assert s["files_indexed"] == 3
    assert s["files_error"] == 0
    assert s["files_pending"] == 0
    assert s["chunks_total"] >= 2  # at least one per text file
    assert s["images_total"] == 1
    assert s["images_unique"] == 1
    assert s["bytes_total"] > 0
    assert s["by_extension"][".md"] == 2
    assert s["by_extension"][".png"] == 1


def test_stats_unique_image_dedup(client: TestClient, tmp_path: Path) -> None:
    """Two files with identical image bytes → 2 image rows, 1 unique sha."""
    png = _png()
    fid = _seed(client, tmp_path / "src", {"a.png": png, "b.png": png})
    s = client.get(f"/api/folders/{fid}/stats").json()
    assert s["images_total"] == 2
    assert s["images_unique"] == 1


def test_stats_unknown_folder_returns_404(client: TestClient) -> None:
    assert client.get("/api/folders/9999/stats").status_code == 404


def test_stats_empty_folder(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "empty"
    src.mkdir()
    fid = client.post("/api/folders", json={"path": str(src)}).json()["id"]
    s = client.get(f"/api/folders/{fid}/stats").json()
    assert s == {
        "folder_id": fid,
        "files_total": 0,
        "files_indexed": 0,
        "files_error": 0,
        "files_pending": 0,
        "chunks_total": 0,
        "images_total": 0,
        "images_unique": 0,
        "bytes_total": 0,
        "by_extension": {},
    }
