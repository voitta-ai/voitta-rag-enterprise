"""_stage / _publish_job_progress emit job.progress for the bound job."""

from __future__ import annotations

import asyncio

import pytest

from voitta_rag_enterprise.logging_config import bind_context
from voitta_rag_enterprise.services import events
from voitta_rag_enterprise.services.indexing import _publish_job_progress, _stage


@pytest.mark.asyncio
async def test_stage_emits_job_progress_for_bound_job(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["jobs"]) as sub:
            with bind_context(job_id=42, kind="extract"):
                with _stage("parse"):
                    pass
                _publish_job_progress("embed_text", 256, 1521)
            await sub.wait(timeout=1.0)
            evs = sub.drain()
        # Coalesced per job_id → only the latest phase survives.
        assert evs[-1]["type"] == "job.progress"
        assert evs[-1]["job_id"] == 42
        assert evs[-1]["phase"] == "embed_text"
        assert evs[-1]["done"] == 256 and evs[-1]["total"] == 1521
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_no_progress_without_bound_job(env: None) -> None:
    """Outside a job (no job_id on context) progress is a no-op — e.g. a
    REST-triggered reindex scan must not emit phantom job.progress."""
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["jobs"]) as sub:
            with _stage("parse"):
                pass
            assert await sub.wait(timeout=0.05) is False
    finally:
        events.uninstall_loop()
