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
    assert s["files_unsupported"] == 0
    assert s["files_pending"] == 0
    assert s["chunks_total"] >= 2  # at least one per text file
    assert s["images_total"] == 1
    assert s["images_unique"] == 1
    assert s["bytes_total"] > 0
    md = s["by_extension"][".md"]
    assert md["files"] == 2 and md["indexed"] == 2 and md["error"] == 0
    assert md["chunks"] >= 2
    png = s["by_extension"][".png"]
    assert png["files"] == 1 and png["indexed"] == 1
    assert png["chunks"] == 0  # standalone images produce no chunks


def test_stats_unique_image_dedup(client: TestClient, tmp_path: Path) -> None:
    """Two files with identical image bytes → 2 image rows, 1 unique sha."""
    png = _png()
    fid = _seed(client, tmp_path / "src", {"a.png": png, "b.png": png})
    s = client.get(f"/api/folders/{fid}/stats").json()
    assert s["images_total"] == 2
    assert s["images_unique"] == 1


def test_stats_unsupported_files_not_counted_as_error(
    client: TestClient, tmp_path: Path
) -> None:
    """Files we don't have a parser for land in ``unsupported`` and are
    surfaced as such in the stats — never as ``error``."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("hello")
    (src / "c.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (src / "d.zzz").write_bytes(b"unknown blob")
    folder_id = client.post("/api/folders", json={"path": str(src)}).json()["id"]
    init_db()
    with session_scope() as s:
        ids = [
            f.id for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars()
        ]
    for fid in ids:
        asyncio.run(run_extract({"file_id": fid}))
        # only the .md will produce embed jobs
        with session_scope() as s:
            f = s.get(File, fid)
            if f and f.state == "extracted":
                asyncio.run(run_embed_text({"file_id": fid}))

    s = client.get(f"/api/folders/{folder_id}/stats").json()
    assert s["files_total"] == 3
    assert s["files_indexed"] == 1
    assert s["files_error"] == 0  # critical: no parser is NOT an error
    assert s["files_unsupported"] == 2
    mp4 = s["by_extension"][".mp4"]
    assert mp4["files"] == 1 and mp4["unsupported"] == 1 and mp4["error"] == 0
    zzz = s["by_extension"][".zzz"]
    assert zzz["files"] == 1 and zzz["unsupported"] == 1 and zzz["error"] == 0


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
        "files_unsupported": 0,
        "files_pending": 0,
        "chunks_total": 0,
        "images_total": 0,
        "images_unique": 0,
        "bytes_total": 0,
        "by_extension": {},
    }
