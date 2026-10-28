"""SQL-vs-Qdrant check and repair (``services/index_repair.py``,
``scripts/repair_index.py``)."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from PIL import Image as PILImage
from qdrant_client.http import models as qm
from sqlalchemy import select, update

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Folder, Image, Job
from voitta_rag_enterprise.services import index_repair, job_queue, vector_store
from voitta_rag_enterprise.services.indexing import (
    HANDLERS,
    run_embed_image,
    run_embed_text,
    run_extract,
)


def _png(color=(10, 20, 30)) -> bytes:
    img = PILImage.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed(root: Path, layout: dict[str, str | bytes]) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content) if isinstance(content, str) else p.write_bytes(content)
    init_db()
    with session_scope() as s:
        folder = Folder(path=str(root), display_name=root.name)
        s.add(folder)
        s.flush()
        folder_id = folder.id
        for rel in layout:
            st = (root / rel).stat()
            s.add(
                File(
                    folder_id=folder_id,
                    rel_path=rel,
                    size_bytes=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    last_seen_at=0,
                    state="pending",
                )
            )
    with session_scope() as s:
        ids = [f.id for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars()]
    for fid in ids:
        asyncio.run(run_extract({"file_id": fid}))
        asyncio.run(run_embed_text({"file_id": fid}))
        with session_scope() as s:
            image_ids = [i.id for i in s.execute(select(Image).where(Image.file_id == fid)).scalars()]
        for iid in image_ids:
            asyncio.run(run_embed_image({"image_id": iid}))
    return folder_id


def _file_id(folder_id: int, rel: str) -> int:
    with session_scope() as s:
        return s.execute(
            select(File.id).where(File.folder_id == folder_id, File.rel_path == rel)
        ).scalar_one()


def _q(fn):
    """Run ``fn(client)`` on the Qdrant thread (embedded Qdrant is
    single-threaded, like the app uses it)."""
    return vector_store.run_on_qdrant(lambda: fn(vector_store.get_client()))


def _drop_points(collection: str, key: str, value: int) -> None:
    _q(lambda c: c.delete(
        collection,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(must=[qm.FieldCondition(key=key, match=qm.MatchValue(value=value))])
        ),
        wait=True,
    ))


def _drain() -> int:
    """Run queued jobs the way the worker does; returns how many ran."""
    n = 0
    while (job := job_queue.claim_one()) is not None:
        asyncio.run(HANDLERS[job.kind](job.payload))
        job_queue.mark_done(job.id)
        n += 1
    return n


def _check(folder_ids=None) -> index_repair.IndexReport:
    with session_scope() as s:
        return index_repair.check(s, folder_ids)


def _repair(report, **kw) -> index_repair.RepairResult:
    with session_scope() as s:
        return index_repair.repair(s, report, **kw)


def _folder(report, folder_id) -> index_repair.FolderReport:
    return next(r for r in report.folders if r.folder_id == folder_id)


def test_healthy_index_reports_ok(env: None, tmp_path: Path) -> None:
    fid = _seed(tmp_path / "docs", {"a.md": "alpha beta", "b.md": "gamma", "logo.png": _png()})
    report = _check()
    assert report.ok
    r = _folder(report, fid)
    assert r.indexed_files == 3 and r.chunks >= 2 and r.figures == 1
    assert _repair(report).job_ids == []


def test_lost_vectors_of_one_file_are_found_and_rebuilt(env: None, tmp_path: Path) -> None:
    """Saturn's pattern: one file of a folder keeps its vectors, another
    lost all of them; the folder still says indexed."""
    fid = _seed(tmp_path / "mck", {"q1.md": "supply chain resilience", "q3.md": "generative ai"})
    other = _seed(tmp_path / "other", {"x.md": "untouched"})
    lost = _file_id(fid, "q1.md")
    _drop_points(vector_store.CHUNKS, "file_id", lost)

    report = _check()
    r = _folder(report, fid)
    assert not report.ok and not r.ok
    assert r.missing_chunks >= 1 and r.stale_chunks == 0
    assert [f for f, _ in r.files_to_embed] == [lost]
    assert _folder(report, other).ok

    result = _repair(report)
    assert result.text_jobs == 1 and result.image_jobs == 0
    with session_scope() as s:
        job = s.get(Job, result.job_ids[0])
        assert job.priority == index_repair.REPAIR_PRIORITY
    assert _drain() == 1
    assert _check().ok
    with session_scope() as s:
        f = s.get(File, lost)
        assert (f.state, f.pending_embeds) == ("indexed", 0)
    assert vector_store.count_points_for_folder(vector_store.CHUNKS, fid) >= 2


def test_repair_job_yields_to_a_reindex_that_started_meanwhile(env: None, tmp_path: Path) -> None:
    fid = _seed(tmp_path / "d", {"a.md": "alpha"})
    a = _file_id(fid, "a.md")
    _drop_points(vector_store.CHUNKS, "file_id", a)
    _repair(_check())
    with session_scope() as s:
        s.execute(update(File).where(File.id == a).values(embed_round=File.embed_round + 1))
    _drain()
    # The job carried the old round, so it aborted rather than racing.
    assert vector_store.count_points_for_folder(vector_store.CHUNKS, fid) == 0


def test_stale_chunk_versions_are_rebuilt(env: None, tmp_path: Path) -> None:
    fid = _seed(tmp_path / "d", {"a.md": "alpha", "b.md": "beta"})
    a = _file_id(fid, "a.md")
    _q(lambda c: c.set_payload(
        vector_store.CHUNKS,
        payload={"dense_model_version": "e5-base-v2@0"},
        points=qm.Filter(must=[qm.FieldCondition(key="file_id", match=qm.MatchValue(value=a))]),
        wait=True,
    ))
    r = _folder(_check(), fid)
    assert r.stale_chunks >= 1 and r.missing_chunks == 0
    assert [f for f, _ in r.files_to_embed] == [a]
    _repair(_check())
    _drain()
    assert _check().ok


def test_lost_image_vectors_are_rebuilt_in_two_passes_for_shared_blobs(
    env: None, tmp_path: Path
) -> None:
    """The same picture in two files is one point. After it is lost, pass 1
    embeds it once, pass 2 attaches the second file."""
    fid = _seed(tmp_path / "pics", {"a.png": _png(), "b.png": _png(), "c.png": _png((200, 0, 0))})
    _q(lambda c: c.delete(
        vector_store.IMAGES, points_selector=qm.FilterSelector(filter=qm.Filter()), wait=True
    ))
    r = _folder(_check(), fid)
    assert r.figures == 2 and r.missing_images == 2

    result = _repair(_check())
    assert result.image_jobs == 2
    _drain()
    r = _folder(_check(), fid)
    assert r.missing_images == 0 and r.unattached_images == 1

    _repair(_check())
    _drain()
    assert _check().ok
    assert vector_store.count_points_for_folder(vector_store.IMAGES, fid) == 2


def test_stale_image_point_is_replaced(env: None, tmp_path: Path) -> None:
    fid = _seed(tmp_path / "pics", {"a.png": _png()})
    _q(lambda c: c.set_payload(
        vector_store.IMAGES,
        payload={"image_model_version": "siglip2-base@0"},
        points=qm.Filter(),
        wait=True,
    ))
    report = _check()
    assert _folder(report, fid).stale_images == 1
    _repair(report)
    _drain()
    assert _check().ok


def _add_defunct_chunk_point(point_id: int) -> None:
    """A point whose chunk row is gone (copy of a live point's vectors)."""
    def _do(c):
        live = c.scroll(vector_store.CHUNKS, limit=1, with_vectors=True, with_payload=True)[0][0]
        c.upsert(
            vector_store.CHUNKS,
            points=[qm.PointStruct(id=point_id, vector=live.vector, payload=live.payload)],
            wait=True,
        )

    _q(_do)


def test_defunct_points_are_deleted(env: None, tmp_path: Path) -> None:
    fid = _seed(tmp_path / "d", {"a.md": "alpha"})
    _add_defunct_chunk_point(987_654)
    report = _check()
    assert report.defunct_chunk_points == [987_654]
    assert _folder(report, fid).ok
    assert _repair(report).deleted_chunk_points == 1
    assert _check().ok
    assert _q(lambda c: c.retrieve(vector_store.CHUNKS, ids=[987_654])) == []


def test_folder_scoped_check_leaves_defunct_points_alone(env: None, tmp_path: Path) -> None:
    fid = _seed(tmp_path / "d", {"a.md": "alpha"})
    _add_defunct_chunk_point(987_654)
    report = _check([fid])
    assert report.ok and report.defunct_chunk_points == []


def test_large_delete_is_refused(env: None, tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path / "d", {"a.md": "alpha", "b.md": "beta", "c.md": "gamma"})
    monkeypatch.setattr(index_repair, "_MIN_DELETE_CAP", 0)
    _add_defunct_chunk_point(987_654)
    report = _check()
    with pytest.raises(index_repair.RepairRefused):
        _repair(report)
    assert _q(lambda c: c.retrieve(vector_store.CHUNKS, ids=[987_654])) != []
    assert _repair(report, allow_large_delete=True).deleted_chunk_points == 1


def test_empty_sql_never_deletes(env: None, tmp_path: Path) -> None:
    """The incident: pointed at a database without the chunks, every point
    looks defunct. Even with the large-delete override nothing goes."""
    _seed(tmp_path / "d", {"a.md": "alpha"})
    with session_scope() as s:
        s.execute(Chunk.__table__.delete())
    report = _check()
    assert report.sql_chunks == 0 and report.defunct_chunk_points
    with pytest.raises(index_repair.RepairRefused):
        _repair(report, allow_large_delete=True)
    assert _q(lambda c: c.count(vector_store.CHUNKS).count) >= 1


def test_empty_file_is_reported_not_repaired(env: None, tmp_path: Path) -> None:
    fid = _seed(tmp_path / "d", {"a.md": "alpha", "blank.md": ""})
    r = _folder(_check(), fid)
    assert r.empty_files == 1 and r.ok


def test_script_checks_and_repairs(env: None, tmp_path: Path, monkeypatch, capsys) -> None:
    from scripts import repair_index

    fid = _seed(tmp_path / "mck", {"q1.md": "supply chain", "q3.md": "ai"})
    _drop_points(vector_store.CHUNKS, "file_id", _file_id(fid, "q1.md"))

    monkeypatch.setattr("sys.argv", ["repair_index"])
    assert repair_index.main() == 1
    assert "1 of 1 folder(s) need repair" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["repair_index", "--repair", "--folder", "mck"])
    assert repair_index.main() == 1
    _drain()
    monkeypatch.setattr("sys.argv", ["repair_index"])
    assert repair_index.main() == 0


def test_wrong_qdrant_is_refused_not_reported_as_all_missing(env: None, tmp_path: Path) -> None:
    """Pointed at a Qdrant without this database's collections (another
    mode, URL or data dir), the check must not call every chunk missing —
    a repair would then re-embed the whole install into the wrong store."""
    _seed(tmp_path / "d", {"a.md": "alpha", "logo.png": _png()})
    _q(lambda c: c.delete_collection(vector_store.CHUNKS))
    with pytest.raises(index_repair.IndexUnreadable, match="does not exist"):
        _check()
    # The operator knows it was lost: then it is all missing, and repair rebuilds it.
    with session_scope() as s:
        report = index_repair.check(s, assume_empty=True)
    assert not report.ok and report.chunk_points == 0
    vector_store._ensured_collections.clear()  # the app restart that recreates it
    _repair(report)
    _drain()
    assert _check().ok


def test_script_refuses_wrong_qdrant(env: None, tmp_path: Path, monkeypatch, capsys) -> None:
    from scripts import repair_index

    _seed(tmp_path / "d", {"a.md": "alpha"})
    _q(lambda c: c.delete_collection(vector_store.CHUNKS))
    monkeypatch.setattr("sys.argv", ["repair_index", "--repair"])
    assert repair_index.main() == 2
    assert "cannot check" in capsys.readouterr().err
