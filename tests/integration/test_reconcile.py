"""Index-health reconcile + ``/folders/{id}/stats.index_health`` field."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder
from voitta_rag_enterprise.services.indexing import (
    run_embed_text,
    run_extract,
)
from voitta_rag_enterprise.services.reconcile import (
    all_folder_health,
    folder_health,
    log_startup_warnings,
)

from ..conftest import auth_as


def _png() -> bytes:
    img = PILImage.new("RGB", (8, 8), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_indexed_folder(
    app, client: TestClient, root: Path, layout: dict[str, str], email: str
) -> int:
    """Register a folder, drive the extract+embed pipeline so it actually
    has Qdrant points. Returns the folder id."""
    auth_as(app, email)
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    r = client.post("/api/folders", json={"name": root.name})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    init_db()
    with session_scope() as s:
        ids = [
            f.id for f in s.execute(select(File).where(File.folder_id == fid)).scalars()
        ]
    for f_id in ids:
        asyncio.run(run_extract({"file_id": f_id}))
        asyncio.run(run_embed_text({"file_id": f_id}))
    return fid


def test_folder_health_ok_after_pipeline(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app) as client:
        fid = _seed_indexed_folder(
            app, client, tmp_path / "a", {"a.md": "hello"}, "alice@x"
        )
        with session_scope() as s:
            folder = s.get(Folder, fid)
            h = folder_health(s, folder)

    assert h.status == "ok"
    assert h.indexed_files == 1
    assert h.qdrant_chunk_points >= 1


def test_folder_health_empty_when_nothing_indexed(env: None, tmp_path: Path) -> None:
    """Registering a folder without seeding any files: indexed_files == 0,
    so the status should be ``empty`` — not ``out_of_sync``. The latter is
    a panic state reserved for "DB says yes, Qdrant says no"."""
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    auth_as(app, "alice@x")
    src = tmp_path / "empty"
    src.mkdir()
    with TestClient(app) as client:
        r = client.post("/api/folders", json={"name": src.name})
        assert r.status_code == 201
        fid = r.json()["id"]
        with session_scope() as s:
            folder = s.get(Folder, fid)
            h = folder_health(s, folder)

    assert h.status == "empty"
    assert h.indexed_files == 0


def test_folder_health_out_of_sync_when_qdrant_wiped(
    env: None, tmp_path: Path
) -> None:
    """The bug we shipped this check for: SQLite says ``state='indexed'`` for
    files but the Qdrant collection has no matching points. Simulate by
    extracting + embedding, then deleting the chunks collection by hand."""
    from voitta_rag_enterprise.main import create_app
    from voitta_rag_enterprise.services import vector_store

    app = create_app()
    with TestClient(app) as client:
        fid = _seed_indexed_folder(
            app, client, tmp_path / "a", {"a.md": "alpha"}, "alice@x"
        )

    # Drop the chunks collection — mirrors what happens when the Qdrant
    # data dir is wiped or the path is changed.
    def _drop():
        client = vector_store.get_client()
        client.delete_collection(vector_store.CHUNKS)

    vector_store.run_on_qdrant(_drop)

    with session_scope() as s:
        folder = s.get(Folder, fid)
        h = folder_health(s, folder)

    assert h.status == "out_of_sync"
    assert h.indexed_files >= 1
    assert h.qdrant_chunk_points == 0


def test_log_startup_warnings_emits_warning_per_out_of_sync_folder(
    env: None, tmp_path: Path
) -> None:
    """Attach a handler directly to the reconcile logger and assert the
    warning lands on it. caplog doesn't help here because the lifespan's
    ``setup_logging`` sets ``propagate=False`` on the voitta_rag_enterprise
    logger tree, so records never reach the root caplog handler."""
    from voitta_rag_enterprise.main import create_app
    from voitta_rag_enterprise.services import vector_store

    app = create_app()
    with TestClient(app) as client:
        _seed_indexed_folder(
            app, client, tmp_path / "a", {"a.md": "alpha"}, "alice@x"
        )

    def _drop():
        client = vector_store.get_client()
        client.delete_collection(vector_store.CHUNKS)

    vector_store.run_on_qdrant(_drop)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("voitta_rag_enterprise.services.reconcile")
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        with session_scope() as s:
            log_startup_warnings(s)
    finally:
        logger.removeHandler(handler)

    assert any(
        "0 chunk points in Qdrant" in r.getMessage() for r in records
    ), [r.getMessage() for r in records]


def test_stats_endpoint_carries_index_health(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app) as client:
        fid = _seed_indexed_folder(
            app, client, tmp_path / "a", {"a.md": "alpha"}, "alice@x"
        )
        r = client.get(f"/api/folders/{fid}/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "index_health" in body
        assert body["index_health"]["status"] == "ok"
        assert body["index_health"]["qdrant_chunk_points"] >= 1


def test_all_folder_health_orders_by_id(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app) as client:
        a = _seed_indexed_folder(app, client, tmp_path / "a", {"a.md": "x"}, "alice@x")
        b = _seed_indexed_folder(app, client, tmp_path / "b", {"b.md": "y"}, "alice@x")

    with session_scope() as s:
        rows = all_folder_health(s)

    ids = [h.folder_id for h in rows]
    assert ids == sorted(ids)
    assert {a, b} <= set(ids)
