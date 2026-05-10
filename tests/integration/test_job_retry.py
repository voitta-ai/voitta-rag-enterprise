"""Tests for the job-retry / cleanup endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder, Job
from voitta_rag_enterprise.services import job_queue


def _make_failed(kind: str = "extract", payload: dict | None = None) -> int:
    init_db()
    with session_scope() as s:
        job_id = job_queue.enqueue(
            s, kind, payload or {"file_id": 1}, dedup_key=f"{kind}:99"
        )
    job_queue.claim_one()
    job_queue.mark_error(job_id, "boom")
    return job_id


def test_retry_failed_job(client: TestClient) -> None:
    job_id = _make_failed()
    r = client.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 200
    new_id = r.json()["new_job_id"]
    assert new_id != job_id

    with session_scope() as s:
        old = s.get(Job, job_id)
        new = s.get(Job, new_id)
        assert old.state == "error"  # original preserved
        assert new.state == "queued"
        assert new.kind == old.kind
        assert new.payload == old.payload


def test_retry_non_failed_returns_400(client: TestClient) -> None:
    init_db()
    with session_scope() as s:
        job_id = job_queue.enqueue(s, "extract", {"file_id": 1})
    r = client.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 400


def test_retry_unknown_returns_404(client: TestClient) -> None:
    init_db()
    r = client.post("/api/jobs/9999/retry")
    assert r.status_code == 404


def test_retry_all_failed(client: TestClient) -> None:
    init_db()
    ids = []
    for i in range(3):
        with session_scope() as s:
            ids.append(
                job_queue.enqueue(s, "extract", {"file_id": i}, dedup_key=f"extract:{i}")
            )
    # Mark them all failed.
    for jid in ids:
        job_queue.claim_one()
        job_queue.mark_error(jid, "fail")

    r = client.post("/api/jobs/retry-failed")
    assert r.status_code == 200
    assert r.json()["retried"] == 3
    with session_scope() as s:
        states = sorted(j.state for j in s.execute(select(Job)).scalars().all())
        # 3 errors (preserved) + 3 queued retries
        assert states == ["error", "error", "error", "queued", "queued", "queued"]


def test_cancel_all_drains_queue(client: TestClient) -> None:
    init_db()
    ids = []
    for i in range(3):
        with session_scope() as s:
            ids.append(
                job_queue.enqueue(
                    s, "extract", {"file_id": i}, dedup_key=f"extract:{i}"
                )
            )

    r = client.post("/api/jobs/cancel-all")
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled_queued"] == 3
    # No MinerU subprocess in the test env, so killed_running stays 0.
    assert body["killed_running"] == 0
    with session_scope() as s:
        rows = s.execute(select(Job).where(Job.id.in_(ids))).scalars().all()
        assert all(j.state == "done" for j in rows)
        assert all(j.error == "cancelled" for j in rows)


def test_cancel_all_no_op_on_quiet_queue(client: TestClient) -> None:
    init_db()
    r = client.post("/api/jobs/cancel-all")
    assert r.status_code == 200
    assert r.json() == {"cancelled_queued": 0, "killed_running": 0}


def test_cleanup_failed_removes_error_rows(client: TestClient) -> None:
    init_db()
    for i in range(3):
        with session_scope() as s:
            jid = job_queue.enqueue(
                s, "extract", {"file_id": i}, dedup_key=f"extract:{i}"
            )
        job_queue.claim_one()
        job_queue.mark_error(jid, "fail")

    r = client.delete("/api/jobs/cleanup-failed")
    assert r.status_code == 200
    assert r.json()["retried"] == 3
    with session_scope() as s:
        assert s.execute(select(Job).where(Job.state == "error")).scalars().all() == []


def _make_file_row(folder_root_name: str, rel_path: str = "a.md") -> int:
    """Create a Folder + File row and return the file_id. Used by the
    cancel-orphan tests below."""
    init_db()
    with session_scope() as s:
        folder = Folder(path=f"/tmp/{folder_root_name}", display_name=folder_root_name)
        s.add(folder)
        s.flush()
        file = File(
            folder_id=folder.id,
            rel_path=rel_path,
            size_bytes=10,
            mtime_ns=0,
            state="pending",
        )
        s.add(file)
        s.flush()
        return file.id


def test_cancel_all_stamps_file_rows_for_extract_jobs(client: TestClient) -> None:
    """Regression: cancel-all on queued extracts must flip the
    associated file row to ``state='error'`` so the file doesn't become
    an orphan that no code path will re-enqueue.

    Before this fix, the live demo accumulated 792 stranded ``pending``
    files after a single cancel-all operation."""
    fid_a = _make_file_row("a")
    fid_b = _make_file_row("b")
    with session_scope() as s:
        job_queue.enqueue(s, "extract", {"file_id": fid_a}, dedup_key=f"extract:{fid_a}")
        job_queue.enqueue(s, "extract", {"file_id": fid_b}, dedup_key=f"extract:{fid_b}")

    r = client.post("/api/jobs/cancel-all")
    assert r.status_code == 200, r.text
    assert r.json()["cancelled_queued"] == 2

    with session_scope() as s:
        rows = s.execute(select(File).where(File.id.in_([fid_a, fid_b]))).scalars().all()
        assert {f.state for f in rows} == {"error"}
        assert {f.error for f in rows} == {"cancelled by user"}
        # embed_round is bumped so a stale embed decrement can't flip the
        # row back to ``indexed`` after this cancel.
        assert all(f.embed_round and f.embed_round >= 1 for f in rows)


def test_cancel_one_queued_stamps_file_row(client: TestClient) -> None:
    """Same regression for the per-job cancel button when the job is
    still queued (not running)."""
    fid = _make_file_row("c")
    with session_scope() as s:
        jid = job_queue.enqueue(
            s, "extract", {"file_id": fid}, dedup_key=f"extract:{fid}"
        )
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 200, r.text

    with session_scope() as s:
        f = s.get(File, fid)
        assert f.state == "error"
        assert f.error == "cancelled by user"


def test_cancel_all_leaves_non_file_jobs_alone(client: TestClient) -> None:
    """A queued ``sync`` or ``reindex_folder`` job has no file_id; cancel
    should still flip the job to done without trying to mark a phantom
    file."""
    init_db()
    with session_scope() as s:
        job_queue.enqueue(s, "sync", {"folder_id": 1}, dedup_key="sync:1")

    r = client.post("/api/jobs/cancel-all")
    assert r.status_code == 200
    # No file rows existed; the cancel just flips the job.
    with session_scope() as s:
        assert s.execute(
            select(Job).where(Job.dedup_key == "sync:1")
        ).scalar_one().state == "done"


def test_reconcile_re_enqueues_stranded_pending(client: TestClient) -> None:
    """Reconcile sweep catches files left in ``pending`` with no live
    extract job — the orphan profile produced by a prior cancel-all."""
    from voitta_rag_enterprise.services.indexing import reconcile_abandoned_extracts

    fid_stranded = _make_file_row("s1")
    fid_with_job = _make_file_row("s2")
    with session_scope() as s:
        # Only fid_with_job has a live extract — fid_stranded is orphaned.
        job_queue.enqueue(
            s, "extract", {"file_id": fid_with_job},
            dedup_key=f"extract:{fid_with_job}",
        )

    repaired = reconcile_abandoned_extracts()
    assert repaired == 1

    with session_scope() as s:
        # Stranded file now has a fresh queued extract job.
        live = s.execute(
            select(Job).where(
                Job.kind == "extract",
                Job.state == "queued",
                Job.dedup_key == f"extract:{fid_stranded}",
            )
        ).scalars().all()
        assert len(live) == 1
        # The file that already had a live job is untouched (no duplicate).
        existing = s.execute(
            select(Job).where(Job.dedup_key == f"extract:{fid_with_job}")
        ).scalars().all()
        assert len(existing) == 1
