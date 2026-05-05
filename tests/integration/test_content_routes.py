"""Tests for /api/files/{id}/text|images, /api/images/{id}, /api/jobs/recent."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy import select

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import File, Image
from voitta_rag_enterprise.services.indexing import run_extract


def _png() -> bytes:
    img = PILImage.new("RGB", (8, 8), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_and_extract(client: TestClient, root: Path, files: dict[str, bytes | str]) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
    folder_id = client.post("/api/folders", json={"name": root.name}).json()["id"]
    with session_scope() as s:
        ids = [f.id for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars()]
    for fid in ids:
        asyncio.run(run_extract({"file_id": fid}))
    return folder_id


def test_get_file_text_returns_extracted_markdown(client: TestClient, tmp_path: Path) -> None:
    _seed_and_extract(client, tmp_path / "src", {"doc.md": "# Hello\n\nworld"})
    fid = client.get("/api/folders/1/files").json()[0]["id"]
    r = client.get(f"/api/files/{fid}/text")
    assert r.status_code == 200
    assert "Hello" in r.text


def test_get_file_text_pre_extraction_returns_409(client: TestClient, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.md").write_text("hi")
    folder_id = client.post("/api/folders", json={"name": src.name}).json()["id"]
    fid = client.get(f"/api/folders/{folder_id}/files").json()[0]["id"]
    # Skipping run_extract — file is still 'pending'.
    assert client.get(f"/api/files/{fid}/text").status_code == 409


def test_get_file_images_lists_extracted_images(client: TestClient, tmp_path: Path) -> None:
    _seed_and_extract(client, tmp_path / "src", {"logo.png": _png()})
    fid = client.get("/api/folders/1/files").json()[0]["id"]
    r = client.get(f"/api/files/{fid}/images")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    img = body[0]
    assert img["mime"] == "image/png"
    assert img["width"] == 8
    assert img["height"] == 8
    # Standalone image: anchor_chunk is None → position is None.
    assert img["position"] is None


def test_get_image_bytes_round_trip(client: TestClient, tmp_path: Path) -> None:
    _seed_and_extract(client, tmp_path / "src", {"logo.png": _png()})
    with session_scope() as s:
        image_id = s.execute(select(Image)).scalar_one().id
    r = client.get(f"/api/images/{image_id}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == _png()


def test_jobs_recent_endpoint(client: TestClient, tmp_path: Path) -> None:
    _seed_and_extract(client, tmp_path / "src", {"a.md": "alpha"})
    r = client.get("/api/jobs/recent?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert len(body) >= 1
    kinds = {j["kind"] for j in body}
    assert "extract" in kinds


def test_jobs_recent_limit_validated(client: TestClient) -> None:
    assert client.get("/api/jobs/recent?limit=0").status_code == 422
    assert client.get("/api/jobs/recent?limit=999").status_code == 422
