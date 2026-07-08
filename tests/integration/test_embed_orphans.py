"""Qdrant point hygiene across re-extracts and mid-embed crashes.

These are the permanent tripwires for the Phase-1 throughput work (payload
indexes + ensure-collection cache + interior wait=False batches) and for
anyone tempted to skip ``replace_chunks_for_file``'s delete-by-filter:
re-extraction assigns NEW chunk ids (SQLite rows are replaced), so without
the unconditional delete a retry after a mid-embed crash strands the old
points forever — duplicate search results that nothing ever cleans up.

Runs against the embedded Qdrant backend via the ``env`` fixture.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Folder
from voitta_rag_enterprise.services import vector_store as vs
from voitta_rag_enterprise.services.indexing import run_extract


def _seed_file(folder_root: Path, rel_path: str, content: str) -> int:
    folder_root.mkdir(parents=True, exist_ok=True)
    abs_path = folder_root / rel_path
    abs_path.write_text(content)
    with session_scope() as s:
        folder = s.execute(
            select(Folder).where(Folder.path == str(folder_root))
        ).scalar_one_or_none()
        if folder is None:
            folder = Folder(path=str(folder_root), display_name=folder_root.name)
            s.add(folder)
            s.flush()
        stat = abs_path.stat()
        f = File(
            folder_id=folder.id,
            rel_path=rel_path,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            last_seen_at=int(time.time()),
            state="pending",
        )
        s.add(f)
        s.flush()
        return f.id


def _point_ids_for_file(file_id: int) -> set[int]:
    """Every chunk-point id Qdrant holds for ``file_id``."""

    def _do() -> set[int]:
        client = vs.get_client()
        if not client.collection_exists(vs.CHUNKS):
            return set()
        out: set[int] = set()
        offset = None
        while True:
            points, offset = client.scroll(
                vs.CHUNKS,
                scroll_filter=vs.qm.Filter(
                    must=[
                        vs.qm.FieldCondition(
                            key="file_id", match=vs.qm.MatchValue(value=file_id)
                        )
                    ]
                ),
                limit=256,
                offset=offset,
                with_payload=False,
            )
            out.update(int(p.id) for p in points)
            if offset is None:
                return out

    return vs.run_on_qdrant(_do)


def _chunk_ids(file_id: int) -> set[int]:
    with session_scope() as s:
        return {
            c.id
            for c in s.execute(
                select(Chunk).where(Chunk.file_id == file_id)
            ).scalars()
        }


def _touch(path: Path, content: str) -> None:
    path.write_text(content)
    # Force a distinct mtime so the change detector can't short-circuit.
    ns = path.stat().st_mtime_ns + 2_000_000_000
    import os

    os.utime(path, ns=(ns, ns))


def test_reextract_shrink_leaves_no_orphan_points(env: None, tmp_path: Path) -> None:
    """Big doc → re-extract as a SMALL doc ⇒ Qdrant holds exactly the new set.

    Note SQLite may REUSE freed chunk rowids, so low ids get overwritten by
    the upsert regardless — the ids that expose a missing delete are the
    surplus HIGH ones from the bigger first generation. Shrinking the doc
    makes those exist; exact Qdrant==SQLite equality is the invariant.
    """
    init_db()
    src = tmp_path / "src"
    file_id = _seed_file(src, "doc.md", "\n\n".join("para " * 80 for _ in range(40)))

    asyncio.run(run_extract({"file_id": file_id}))
    first_points = _point_ids_for_file(file_id)
    assert first_points == _chunk_ids(file_id)
    assert len(first_points) > 1  # need surplus ids for the shrink to bite

    with session_scope() as s:
        s.get(File, file_id).state = "pending"
        s.commit()
    _touch(src / "doc.md", "one tiny paragraph")
    asyncio.run(run_extract({"file_id": file_id}))

    second_chunks = _chunk_ids(file_id)
    assert len(second_chunks) < len(first_points)
    # Exactly the new generation — no stale survivors from the big one.
    assert _point_ids_for_file(file_id) == second_chunks


def test_mid_embed_crash_then_retry_converges(
    env: None, tmp_path: Path, monkeypatch
) -> None:
    """Crash AFTER points hit Qdrant but BEFORE state flips ⇒ retry heals.

    Simulates the worst window: ``replace_chunks_for_file`` fully applied,
    then the process dies (the SQLite decrement never runs). The retry
    re-extracts (new chunk ids) — the unconditional delete must clear the
    crashed generation's points or search returns both generations.
    """
    init_db()
    src = tmp_path / "src"
    file_id = _seed_file(
        src, "doc.md", "\n\n".join("gamma " * 80 for _ in range(40))
    )

    real_replace = vs.replace_chunks_for_file
    calls = {"n": 0}

    def _crash_after_upsert(fid: int, points) -> None:
        real_replace(fid, points)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated kill -9 after upsert")

    # indexing does ``from . import vector_store`` at call time, so patching
    # the module attribute is enough.
    monkeypatch.setattr(vs, "replace_chunks_for_file", _crash_after_upsert)

    asyncio.run(run_extract({"file_id": file_id}))  # embeds inline; crashes
    with session_scope() as s:
        assert s.get(File, file_id).state == "error"
    crashed_points = _point_ids_for_file(file_id)
    assert crashed_points  # the window is real: points landed, state didn't

    # Operator retry (reindex path resets to pending and re-runs extract).
    # The retry doc is much SMALLER: without the unconditional delete, the
    # crashed generation's surplus ids would survive as orphans.
    with session_scope() as s:
        f = s.get(File, file_id)
        f.state = "pending"
        f.error = None
        s.commit()
    _touch(src / "doc.md", "one small retry paragraph")
    asyncio.run(run_extract({"file_id": file_id}))

    with session_scope() as s:
        f = s.get(File, file_id)
        assert f.state == "indexed" and f.pending_embeds == 0
    final_chunks = _chunk_ids(file_id)
    assert len(final_chunks) < len(crashed_points)
    # Qdrant holds exactly the retry generation — the crash left nothing.
    assert _point_ids_for_file(file_id) == final_chunks


def test_multi_batch_file_fully_applied(
    env: None, tmp_path: Path, monkeypatch
) -> None:
    """>_UPSERT_BATCH_POINTS chunks (interior batches wait=False): every
    point must still be durably present once the file reports indexed.

    The batch cap is lowered for the test so a modest corpus spans several
    batches — what matters is exercising the interior-wait=False path, not
    generating 256+ real chunks.
    """
    init_db()
    monkeypatch.setattr(vs, "_UPSERT_BATCH_POINTS", 32)
    paras = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(600))
    file_id = _seed_file(tmp_path / "src", "big.md", paras)

    asyncio.run(run_extract({"file_id": file_id}))

    chunk_ids = _chunk_ids(file_id)
    assert len(chunk_ids) > vs._UPSERT_BATCH_POINTS, (
        "test corpus too small to exercise multi-batch interior writes"
    )
    with session_scope() as s:
        f = s.get(File, file_id)
        assert f.state == "indexed" and f.pending_embeds == 0
    assert _point_ids_for_file(file_id) == chunk_ids
