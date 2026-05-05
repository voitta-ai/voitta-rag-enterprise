"""Tests for the SQLite-backed job queue."""

from __future__ import annotations

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Job
from voitta_rag_enterprise.services import job_queue


def test_enqueue_assigns_id_and_persists(env: None) -> None:
    init_db()
    with session_scope() as s:
        job_id = job_queue.enqueue(s, "extract", {"file_id": 7})
    assert job_id > 0
    with session_scope() as s:
        j = s.get(Job, job_id)
        assert j is not None
        assert j.kind == "extract"
        assert j.state == "queued"
        assert j.attempts == 0


def test_enqueue_dedup_returns_existing_id(env: None) -> None:
    init_db()
    with session_scope() as s:
        first = job_queue.enqueue(
            s, "extract", {"file_id": 1}, dedup_key="extract:1"
        )
    with session_scope() as s:
        for _ in range(5):
            again = job_queue.enqueue(
                s, "extract", {"file_id": 1}, dedup_key="extract:1"
            )
            assert again == first
    with session_scope() as s:
        assert s.query(Job).count() == 1


def test_enqueue_after_finished_creates_new_job(env: None) -> None:
    init_db()
    with session_scope() as s:
        first = job_queue.enqueue(s, "extract", {"file_id": 2}, dedup_key="extract:2")
    job_queue.mark_done(first)

    with session_scope() as s:
        second = job_queue.enqueue(s, "extract", {"file_id": 2}, dedup_key="extract:2")
    assert second != first
    with session_scope() as s:
        assert s.query(Job).count() == 2


def test_claim_one_picks_up_queued_job(env: None) -> None:
    init_db()
    with session_scope() as s:
        jid = job_queue.enqueue(s, "extract", {"file_id": 1})
    claimed = job_queue.claim_one()
    assert claimed is not None
    assert claimed.id == jid
    assert claimed.kind == "extract"
    assert claimed.payload == {"file_id": 1}
    assert claimed.attempts == 1
    with session_scope() as s:
        assert s.get(Job, jid).state == "running"


def test_claim_one_returns_none_when_empty(env: None) -> None:
    init_db()
    assert job_queue.claim_one() is None


def test_claim_one_orders_by_priority_then_id(env: None) -> None:
    init_db()
    with session_scope() as s:
        a = job_queue.enqueue(s, "extract", {"x": "a"}, priority=0)
        b = job_queue.enqueue(s, "extract", {"x": "b"}, priority=10)
        c = job_queue.enqueue(s, "extract", {"x": "c"}, priority=10)
    first = job_queue.claim_one()
    second = job_queue.claim_one()
    third = job_queue.claim_one()
    assert (first.id, second.id, third.id) == (b, c, a)


def test_mark_done(env: None) -> None:
    init_db()
    with session_scope() as s:
        jid = job_queue.enqueue(s, "extract", {})
    job_queue.claim_one()
    job_queue.mark_done(jid)
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.state == "done"
        assert j.finished_at is not None


def test_mark_error(env: None) -> None:
    init_db()
    with session_scope() as s:
        jid = job_queue.enqueue(s, "extract", {})
    job_queue.claim_one()
    job_queue.mark_error(jid, "boom")
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.state == "error"
        assert j.error == "boom"


def test_mark_done_unknown_id_is_noop(env: None) -> None:
    init_db()
    job_queue.mark_done(9999)


def test_dedup_collapses_after_claim(env: None) -> None:
    """A job in 'running' state still blocks new enqueues with the same key."""
    init_db()
    with session_scope() as s:
        first = job_queue.enqueue(s, "extract", {"file_id": 5}, dedup_key="extract:5")
    claimed = job_queue.claim_one()
    assert claimed.id == first
    with session_scope() as s:
        again = job_queue.enqueue(s, "extract", {"file_id": 5}, dedup_key="extract:5")
    assert again == first
    with session_scope() as s:
        assert s.query(Job).count() == 1
