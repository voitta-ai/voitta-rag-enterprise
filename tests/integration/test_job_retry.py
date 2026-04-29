"""Tests for the job-retry / cleanup endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import Job
from voitta_image_rag.services import job_queue


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
