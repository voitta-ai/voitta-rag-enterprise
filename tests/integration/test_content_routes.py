"""Tests for /api/files/{id}/text|images, /api/images/{id}, /api/jobs/recent."""

from __future__ import annotations

import asyncio
import io
import time
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


def test_jobs_recent_always_includes_running_job(
    client: TestClient, tmp_path: Path,
) -> None:
    """Regression: a queue of N freshly-enqueued jobs would push the
    actually-running job (older id) off the bottom of the recent
    window. The endpoint must always include running rows in addition
    to the most-recent slice."""
    from voitta_rag_enterprise.db.models import Job
    from voitta_rag_enterprise.db.database import session_scope

    _seed_and_extract(client, tmp_path / "src", {"a.md": "alpha"})

    # Synthesise: one running job (oldest id), then a burst of newer
    # queued jobs that would otherwise mask it.
    with session_scope() as s:
        # Mark an existing job as running so its id is the oldest.
        first = s.execute(
            select(Job).order_by(Job.id).limit(1)
        ).scalar_one()
        first.state = "running"
        first.started_at = 1
        s.flush()
        running_id = first.id
        # Add N queued jobs all with newer ids than ``running_id``.
        for i in range(40):
            s.add(
                Job(
                    kind="extract",
                    payload="{}",
                    state="queued",
                    enqueued_at=int(time.time()) + i,
                )
            )
        s.commit()

    r = client.get("/api/jobs/recent?limit=10")
    assert r.status_code == 200
    body = r.json()
    ids = [j["id"] for j in body]
    assert running_id in ids, (
        f"running job {running_id} dropped from /recent (limit=10) — got ids {ids}"
    )
    # Running pinned first.
    assert body[0]["id"] == running_id
    assert body[0]["state"] == "running"


def test_jobs_recent_resolves_display_path_for_extract(
    client: TestClient, tmp_path: Path,
) -> None:
    """``display_path`` is filled in for jobs whose payload references a
    file, so the SPA can show ``extract #N — folder/file.md`` without a
    second round-trip."""
    _seed_and_extract(client, tmp_path / "src", {"docs/intro.md": "hi"})
    r = client.get("/api/jobs/recent?limit=10")
    body = r.json()
    extract_jobs = [j for j in body if j["kind"] == "extract"]
    assert extract_jobs
    paths = [j["display_path"] for j in extract_jobs]
    assert any(p == "docs/intro.md" for p in paths), paths


def test_jobs_recent_display_path_none_when_payload_lacks_file_id(
    client: TestClient, tmp_path: Path,
) -> None:
    """``sync`` jobs reference a folder, not a file — display_path stays
    None. The SPA hides the path line in that case."""
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import Job
    import json as _json

    _seed_and_extract(client, tmp_path / "src", {"a.md": "x"})
    with session_scope() as s:
        s.add(
            Job(
                kind="sync",
                payload=_json.dumps({"folder_id": 1}),
                state="queued",
                enqueued_at=int(time.time()),
            )
        )
        s.commit()

    r = client.get("/api/jobs/recent?limit=20")
    body = r.json()
    sync_jobs = [j for j in body if j["kind"] == "sync"]
    assert sync_jobs
    assert all(j["display_path"] is None for j in sync_jobs)
