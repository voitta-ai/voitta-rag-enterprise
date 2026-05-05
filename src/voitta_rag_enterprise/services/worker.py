"""Async worker pool consuming the job queue.

Stage 2 ships placeholder handlers that just succeed. Each subsequent stage
overrides the relevant handler when its functionality lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from ..logging_config import bind_context
from . import job_queue

logger = logging.getLogger(__name__)

JobHandler = Callable[[dict], Awaitable[None]]


async def _noop(payload: dict) -> None:
    logger.debug("noop handler payload=%s", payload)


DEFAULT_HANDLERS: dict[str, JobHandler] = {
    "extract": _noop,
    "embed_text": _noop,
    "embed_image": _noop,
    "delete_file": _noop,
    "reindex_folder": _noop,
    "gc_cas": _noop,
}


class WorkerPool:
    def __init__(
        self,
        size: int,
        handlers: dict[str, JobHandler] | None = None,
        idle_sleep: float = 0.2,
    ) -> None:
        self._size = max(1, size)
        self._handlers = handlers if handlers is not None else DEFAULT_HANDLERS
        self._idle_sleep = idle_sleep
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for i in range(self._size):
            self._tasks.append(asyncio.create_task(self._run(f"worker-{i}")))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, name: str) -> None:
        logger.info("%s started", name)
        try:
            while not self._stop.is_set():
                claimed = await asyncio.to_thread(job_queue.claim_one)
                if claimed is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self._idle_sleep)
                    continue
                handler = self._handlers.get(claimed.kind)
                if handler is None:
                    await asyncio.to_thread(
                        job_queue.mark_error, claimed.id, f"no handler for {claimed.kind}"
                    )
                    continue
                with bind_context(job_id=claimed.id, kind=claimed.kind):
                    logger.info("%s claim job", name)
                    try:
                        await handler(claimed.payload)
                        await asyncio.to_thread(job_queue.mark_done, claimed.id)
                        logger.info("%s job done", name)
                    except Exception as e:
                        logger.exception("%s job failed", name)
                        await asyncio.to_thread(job_queue.mark_error, claimed.id, str(e))
        finally:
            logger.info("%s stopped", name)
