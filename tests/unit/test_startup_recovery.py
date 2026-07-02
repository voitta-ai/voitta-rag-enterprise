"""Tests for the disk-aware startup recovery sweep."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Folder, Job
from voitta_rag_enterprise.services import job_queue
from voitta_rag_enterprise.services.startup_recovery import run_startup_recovery


def _mk_folder(tmp_path: Path) -> int:
    root = tmp_path / "docs"
    root.mkdir(parents=True, exist_ok=True)
    with session_scope() as s:
        folder = Folder(path=str(root), display_name="docs")
        s.add(folder)
        s.flush()
        return folder.id


def _mk_file(folder_id: int, rel: str, **kw) -> int:
    with session_scope() as s:
        f = File(folder_id=folder_id, rel_path=rel, **kw)
        s.add(f)
        s.flush()
        return f.id


def _folder_root(folder_id: int) -> Path:
    with session_scope() as s:
        return Path(s.get(Folder, folder_id).path)


def _file_state(file_id: int) -> tuple[str | None, str | None]:
    with session_scope() as s:
        f = s.get(File, file_id)
        return (f.state, f.file_cas_id) if f is not None else (None, None)


def _queued_jobs(kind: str) -> list[Job]:
    with session_scope() as s:
        return list(
            s.execute(
                select(Job).where(Job.kind == kind, Job.state == "queued")
            ).scalars()
        )


def test_healthy_db_is_untouched(env: None, tmp_path: Path) -> None:
    init_db()
    folder_id = _mk_folder(tmp_path)
    (_folder_root(folder_id) / "a.txt").write_text("hello")
    fid = _mk_file(folder_id, "a.txt", state="indexed", file_cas_id="sha-a")

    report = run_startup_recovery()

    assert report.total() == 0
    assert _file_state(fid) == ("indexed", "sha-a")


def test_delete_job_cancelled_when_file_on_disk(env: None, tmp_path: Path) -> None:
    """The restart-makes-it-worse case: a raced scan flagged an on-disk file
    deleted and queued a delete; recovery must cancel it and resurrect."""
    init_db()
    folder_id = _mk_folder(tmp_path)
    (_folder_root(folder_id) / "a.txt").write_text("hello")
    fid = _mk_file(folder_id, "a.txt", state="deleted")
    with session_scope() as s:
        s.add(Chunk(file_id=fid, chunk_index=0, chunk_hash="h", text="t"))
        job_queue.enqueue(s, "delete_file", {"file_id": fid}, dedup_key=f"delete:{fid}")

    report = run_startup_recovery()

    assert report.cancelled_delete_jobs == 1
    assert report.resurrected_files == 1
    assert _file_state(fid)[0] == "pending"
    assert not _queued_jobs("delete_file")
    assert len(_queued_jobs("extract")) == 1


def test_interrupted_delete_is_completed(env: None, tmp_path: Path) -> None:
    """Row flagged deleted, file really gone, delete job lost -> re-enqueue."""
    init_db()
    folder_id = _mk_folder(tmp_path)
    fid = _mk_file(folder_id, "gone.txt", state="deleted")

    report = run_startup_recovery()

    assert report.completed_deletes == 1
    jobs = _queued_jobs("delete_file")
    assert len(jobs) == 1
    assert f'"file_id": {fid}' in jobs[0].payload


def test_orphan_jobs_cancelled(env: None, tmp_path: Path) -> None:
    init_db()
    _mk_folder(tmp_path)
    with session_scope() as s:
        job_queue.enqueue(s, "extract", {"file_id": 99999}, dedup_key="extract:99999")
        job_queue.enqueue(s, "sync", {"folder_id": 99999})

    report = run_startup_recovery()

    assert report.cancelled_orphan_jobs == 2
    assert not _queued_jobs("extract")
    assert not _queued_jobs("sync")


def test_resurrect_without_chunks_forces_full_reextract(
    env: None, tmp_path: Path
) -> None:
    """Chunks wiped by an interrupted delete: the CAS pointer must be dropped
    so the fresh extract can't sha-short-circuit to 'indexed' with an empty
    index behind it."""
    init_db()
    folder_id = _mk_folder(tmp_path)
    (_folder_root(folder_id) / "a.txt").write_text("hello")
    fid = _mk_file(folder_id, "a.txt", state="deleted", file_cas_id="stale-sha")

    run_startup_recovery()

    state, cas = _file_state(fid)
    assert state == "pending"
    assert cas is None


def test_indexed_counter_drift_snapped(env: None, tmp_path: Path) -> None:
    init_db()
    folder_id = _mk_folder(tmp_path)
    (_folder_root(folder_id) / "a.txt").write_text("hello")
    fid = _mk_file(
        folder_id, "a.txt", state="indexed", file_cas_id="sha", pending_embeds=3
    )

    report = run_startup_recovery()

    assert report.snapped_counters == 1
    with session_scope() as s:
        assert s.get(File, fid).pending_embeds == 0


def test_retryable_error_requeued_parser_error_kept(
    env: None, tmp_path: Path
) -> None:
    init_db()
    folder_id = _mk_folder(tmp_path)
    root = _folder_root(folder_id)
    (root / "io.txt").write_text("x")
    (root / "bad.txt").write_text("x")
    io_id = _mk_file(folder_id, "io.txt", state="error", error="read failed: EIO")
    bad_id = _mk_file(
        folder_id, "bad.txt", state="error", error="PdfParser.parse raised\n..."
    )

    report = run_startup_recovery()

    assert report.retried_errors == 1
    assert _file_state(io_id)[0] == "pending"
    assert _file_state(bad_id)[0] == "error"


def test_embed_failure_retry_cannot_heal_to_indexed(
    env: None, tmp_path: Path
) -> None:
    """A failed inline embed leaves chunks committed but vectors missing,
    tracked by pending_embeds > 0. The retry must preserve that counter —
    zeroing it would let the re-extract sha-short-circuit straight to
    'indexed' with no vectors behind it."""
    init_db()
    folder_id = _mk_folder(tmp_path)
    (_folder_root(folder_id) / "a.txt").write_text("hello")
    fid = _mk_file(
        folder_id,
        "a.txt",
        state="error",
        error="inline text embed failed\nTraceback ...",
        file_cas_id="sha-a",
        pending_embeds=3,
    )
    with session_scope() as s:
        s.add(Chunk(file_id=fid, chunk_index=0, chunk_hash="h", text="t"))

    report = run_startup_recovery()

    assert report.retried_errors == 1
    with session_scope() as s:
        f = s.get(File, fid)
        assert f.state == "pending"
        assert f.pending_embeds == 3     # preserved -> forces full re-extract
        assert f.file_cas_id == "sha-a"  # chunks intact -> pointer kept
    assert len(_queued_jobs("extract")) == 1


def test_disabled_folder_left_alone(env: None, tmp_path: Path) -> None:
    """Disabled folders are outside scan/watch scope; recovery must not
    re-initiate work for them either."""
    init_db()
    root = tmp_path / "off"
    root.mkdir()
    (root / "a.txt").write_text("x")
    with session_scope() as s:
        folder = Folder(path=str(root), display_name="off", enabled=False)
        s.add(folder)
        s.flush()
        folder_id = folder.id
    fid = _mk_file(folder_id, "a.txt", state="deleted")

    report = run_startup_recovery()

    assert report.total() == 0
    assert _file_state(fid)[0] == "deleted"
    assert not _queued_jobs("extract")
    assert not _queued_jobs("delete_file")


def test_unreachable_folder_root_left_alone(env: None, tmp_path: Path) -> None:
    """Missing mount must never translate into deletes or resurrections."""
    init_db()
    with session_scope() as s:
        folder = Folder(path=str(tmp_path / "not-mounted"), display_name="nfs")
        s.add(folder)
        s.flush()
        folder_id = folder.id
    fid = _mk_file(folder_id, "a.txt", state="deleted")

    report = run_startup_recovery()

    assert report.total() == 0
    assert _file_state(fid)[0] == "deleted"
    assert not _queued_jobs("delete_file")
