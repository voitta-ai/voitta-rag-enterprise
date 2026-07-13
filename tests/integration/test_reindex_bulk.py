"""Reindex_folder: bulk wipe + progress events.

The previous per-file loop took ~6 minutes on a 2k-file folder. The new
bulk path uses one folder-scope Qdrant delete and chunked SQL DELETEs,
plus emits ``folder.reindex_progress`` events the SPA renders as a live
"Wiping… 600/1969" pill.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Image
from voitta_rag_enterprise.services import events as events_mod
from voitta_rag_enterprise.services.indexing import (
    run_embed_image,
    run_embed_text,
    run_extract,
    run_reindex_folder,
)

from ..conftest import auth_as


def _png() -> bytes:
    img = PILImage.new("RGB", (8, 8), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _index_files(folder_id: int) -> None:
    """Run extract → embed for every pending file in a folder synchronously.

    Walks the same path that the worker pool would, but inline so tests
    don't have to spin up the queue.
    """
    init_db()
    with session_scope() as s:
        ids = [
            f.id
            for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars()
        ]
    for fid in ids:
        asyncio.run(run_extract({"file_id": fid}))
        asyncio.run(run_embed_text({"file_id": fid}))
        with session_scope() as s:
            for img in s.execute(select(Image).where(Image.file_id == fid)).scalars():
                asyncio.run(run_embed_image({"image_id": img.id}))


def _setup_folder(tmp_path: Path, app, client: TestClient, n_files: int = 5) -> int:
    """Register a folder seeded with ``n_files`` indexable .md files.

    Returns the new folder id. Used as a fixture for both the bulk-wipe
    correctness check and the progress-event ordering test.
    """
    auth_as(app, "alice@x")
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (src / f"f{i}.md").write_text(f"file {i} body alpha bravo charlie")
    r = client.post("/api/folders", json={"name": src.name})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    _index_files(fid)
    return fid


# ---------------------------------------------------------------------------
# Bulk wipe correctness
# ---------------------------------------------------------------------------


def test_reindex_bulk_wipe_clears_chunks_and_images(env: None, tmp_path: Path) -> None:
    """After a folder reindex, every Chunk + Image row for the folder should
    be gone (the worker re-extracts and rewrites them downstream; we don't
    drive that here). Catches any path where the bulk SQL DELETE would
    miss rows because of an in-memory ORM state mismatch."""
    from voitta_rag_enterprise.main import create_app
    from voitta_rag_enterprise.services import vector_store

    app = create_app()
    with TestClient(app) as client:
        fid = _setup_folder(tmp_path, app, client)
        with session_scope() as s:
            file_ids = [
                f.id
                for f in s.execute(select(File).where(File.folder_id == fid)).scalars()
            ]
            chunks_before = s.execute(
                select(Chunk).where(Chunk.file_id.in_(file_ids))
            ).all()
        assert chunks_before, "extract should have produced chunks"
        # And Qdrant should have at least one point for this folder.
        points_before = vector_store.count_points_for_folder(
            vector_store.CHUNKS, fid
        )
        assert points_before >= 1

        asyncio.run(
            run_reindex_folder({"folder_id": fid, "file_ids": file_ids})
        )

    # SQLite chunk + image rows for these files: gone.
    with session_scope() as s:
        chunks_after = s.execute(
            select(Chunk).where(Chunk.file_id.in_(file_ids))
        ).all()
        images_after = s.execute(
            select(Image).where(Image.file_id.in_(file_ids))
        ).all()
        files_after = (
            s.execute(select(File).where(File.folder_id == fid)).scalars().all()
        )
    assert chunks_after == []
    assert images_after == []
    # Files remain — but every row is reset to pending with cleared CAS.
    assert all(f.state == "pending" for f in files_after)
    assert all(f.file_cas_id is None for f in files_after)

    # Qdrant chunk points for the folder: also gone in a single bulk call.
    points_after = vector_store.count_points_for_folder(vector_store.CHUNKS, fid)
    assert points_after == 0


# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------


def test_reindex_emits_progress_events_in_phase_order(
    env: None, tmp_path: Path
) -> None:
    """The wipe path publishes folder.reindex_progress events at the start
    of each phase, mid-phase per chunk, and a final phase=done. The SPA
    relies on the order (cancelling → wiping → queueing → done) so the
    "Wiping…" pill flips through the right verbs."""
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app) as client:
        fid = _setup_folder(tmp_path, app, client, n_files=3)
        with session_scope() as s:
            file_ids = [
                f.id
                for f in s.execute(select(File).where(File.folder_id == fid)).scalars()
            ]

        captured: list[dict] = []

        # The events dispatch tries to call into the running asyncio loop;
        # easier to grab events at the publish() seam.
        original_publish = events_mod.publish

        def _capture(topic: str, event: dict) -> None:
            if topic == "folders" and event.get("type") == "folder.reindex_progress":
                captured.append(event)
            original_publish(topic, event)

        events_mod.publish = _capture
        try:
            asyncio.run(
                run_reindex_folder({"folder_id": fid, "file_ids": file_ids})
            )
        finally:
            events_mod.publish = original_publish

    phases = [e["phase"] for e in captured]
    # First "cancelling" phase always emits one event (with done==total).
    assert "cancelling" in phases
    # Wipe phase + queue phase fire at least one mid-progress event each.
    assert "wiping" in phases
    assert "queueing" in phases
    # Last event is always phase=done.
    assert phases[-1] == "done"
    # Order: every cancelling event precedes the first wiping; every wiping
    # precedes the first queueing; every queueing precedes done.
    def _first(p: str) -> int:
        return phases.index(p)

    assert _first("cancelling") < _first("wiping") < _first("queueing") < _first("done")

    # Final event reports total == file count and done == total (the SPA
    # uses these to clear the badge cleanly).
    last = captured[-1]
    assert last["total"] == len(file_ids)
    assert last["done"] == last["total"]
    assert last["folder_id"] == fid


def test_reindex_progress_chunks_size_param(
    env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a small chunk size, we should see multiple ``wiping`` events —
    not just the start + final one. Confirms the per-chunk progress emit
    actually fires inside the loop."""
    from voitta_rag_enterprise.main import create_app

    monkeypatch.setattr(
        "voitta_rag_enterprise.services.indexing.reindex._REINDEX_PROGRESS_CHUNK", 2
    )

    app = create_app()
    with TestClient(app) as client:
        fid = _setup_folder(tmp_path, app, client, n_files=5)
        with session_scope() as s:
            file_ids = [
                f.id
                for f in s.execute(select(File).where(File.folder_id == fid)).scalars()
            ]

        captured: list[dict] = []
        original_publish = events_mod.publish

        def _capture(topic: str, event: dict) -> None:
            if topic == "folders" and event.get("type") == "folder.reindex_progress":
                captured.append(event)
            original_publish(topic, event)

        events_mod.publish = _capture
        try:
            asyncio.run(
                run_reindex_folder({"folder_id": fid, "file_ids": file_ids})
            )
        finally:
            events_mod.publish = original_publish

    wipe_events = [e for e in captured if e["phase"] == "wiping"]
    # 5 files, chunk=2 → 3 chunks → 1 reset + 3 chunked emits = 4 wipe events.
    # Don't pin the exact count (would over-couple to the loop shape); just
    # require the per-chunk emits actually happened.
    assert len(wipe_events) >= 3
    # ``done`` monotonically increases within the wipe phase.
    wipe_dones = [e["done"] for e in wipe_events]
    assert wipe_dones == sorted(wipe_dones)
    assert wipe_events[-1]["done"] == 5


# ---------------------------------------------------------------------------
# State-scoped reindex ("reindex light")
# ---------------------------------------------------------------------------


def test_reindex_states_filter_targets_only_matching_files(
    env: None, tmp_path: Path
) -> None:
    """POST /reindex with states=['error','unsupported'] schedules only the
    failed/skipped rows: they flip to pending immediately, while indexed
    rows keep their state (and their chunks — nothing is wiped for them)."""
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app) as client:
        fid = _setup_folder(tmp_path, app, client, n_files=4)
        # Park two files as if they had failed / been skipped.
        with session_scope() as s:
            rows = (
                s.execute(
                    select(File).where(File.folder_id == fid).order_by(File.id)
                ).scalars().all()
            )
            rows[0].state = "error"
            rows[0].error = "extract crashed: boom"
            rows[1].state = "unsupported"
            parked_ids = {rows[0].id, rows[1].id}
            indexed_ids = {rows[2].id, rows[3].id}
            s.commit()

        r = client.post(
            f"/api/folders/{fid}/reindex",
            json={"states": ["error", "unsupported"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["scheduled"] == 2

        with session_scope() as s:
            rows = (
                s.execute(select(File).where(File.folder_id == fid)).scalars().all()
            )
            by_id = {f.id: f for f in rows}
        # Targeted rows flipped to pending (error cleared) by the endpoint.
        for pid in parked_ids:
            assert by_id[pid].state == "pending"
            assert by_id[pid].error is None
        # Untargeted rows untouched.
        for iid in indexed_ids:
            assert by_id[iid].state == "indexed"

        # Bogus state names are rejected up front.
        r = client.post(f"/api/folders/{fid}/reindex", json={"states": ["bogus"]})
        assert r.status_code == 400
        # And deleted rows can't be targeted through this door.
        r = client.post(f"/api/folders/{fid}/reindex", json={"states": ["deleted"]})
        assert r.status_code == 400
