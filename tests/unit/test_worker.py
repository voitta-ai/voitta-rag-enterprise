"""Tests for the WorkerPool."""

from __future__ import annotations

import asyncio

import pytest

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Job
from voitta_rag_enterprise.services import job_queue
from voitta_rag_enterprise.services.worker import WorkerPool


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out waiting for: {predicate}")


def _job_state(job_id: int) -> str:
    with session_scope() as s:
        return s.get(Job, job_id).state


@pytest.mark.asyncio
async def test_worker_processes_queued_job(env: None) -> None:
    init_db()
    seen: list[dict] = []

    async def handle(payload: dict) -> None:
        seen.append(payload)

    pool = WorkerPool(size=1, handlers={"extract": handle}, idle_sleep=0.05)
    with session_scope() as s:
        jid = job_queue.enqueue(s, "extract", {"x": 1})

    await pool.start()
    try:
        await _wait_until(lambda: _job_state(jid) == "done")
    finally:
        await pool.stop()
    assert seen == [{"x": 1}]


@pytest.mark.asyncio
async def test_worker_persists_handler_result(env: None) -> None:
    """A handler's return dict is persisted on the job (jobs.result) so the
    Jobs panel can show the detail (e.g. sync stats)."""
    init_db()

    async def handle(payload: dict) -> dict:
        return {"files_added": 3, "errors": []}

    pool = WorkerPool(size=1, handlers={"sync": handle}, idle_sleep=0.05)
    with session_scope() as s:
        jid = job_queue.enqueue(s, "sync", {"folder_id": 1})

    await pool.start()
    try:
        await _wait_until(lambda: _job_state(jid) == "done")
    finally:
        await pool.stop()

    import json

    with session_scope() as s:
        j = s.get(Job, jid)
        assert json.loads(j.result) == {"files_added": 3, "errors": []}


@pytest.mark.asyncio
async def test_worker_marks_error_on_handler_exception(env: None) -> None:
    init_db()

    async def boom(payload: dict) -> None:
        raise RuntimeError("nope")

    pool = WorkerPool(size=1, handlers={"extract": boom}, idle_sleep=0.05)
    with session_scope() as s:
        jid = job_queue.enqueue(s, "extract", {})

    await pool.start()
    try:
        await _wait_until(lambda: _job_state(jid) == "error")
    finally:
        await pool.stop()

    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.error == "nope"


@pytest.mark.asyncio
async def test_worker_marks_error_for_unknown_kind(env: None) -> None:
    init_db()
    pool = WorkerPool(size=1, handlers={}, idle_sleep=0.05)
    with session_scope() as s:
        jid = job_queue.enqueue(s, "mystery", {})

    await pool.start()
    try:
        await _wait_until(lambda: _job_state(jid) == "error")
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_workers_share_queue_across_pool(env: None) -> None:
    init_db()
    processed: list[int] = []

    async def collect(payload: dict) -> None:
        processed.append(payload["i"])

    pool = WorkerPool(size=3, handlers={"extract": collect}, idle_sleep=0.02)
    with session_scope() as s:
        for i in range(10):
            job_queue.enqueue(s, "extract", {"i": i})

    await pool.start()
    try:
        await _wait_until(lambda: len(processed) == 10, timeout=3.0)
    finally:
        await pool.stop()

    assert sorted(processed) == list(range(10))
