"""In-process event broker for WebSocket fan-out.

Publishers call :func:`publish` from any thread; the broker schedules a
non-blocking put on each subscribed asyncio.Queue using the lifespan-installed
event loop. WebSocket handlers create a :class:`Subscription` and ``await`` its
queue.

Topics in use:
- ``folders``  — ``folder.added``, ``folder.removed``
- ``files``    — ``file.upserted``, ``file.deleted``
- ``jobs``     — ``job.started``, ``job.finished``
- ``stats``    — periodic counters (Stage 8 lands the publisher)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

_topics: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def install_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def uninstall_loop() -> None:
    global _loop, _topics
    _loop = None
    with _lock:
        _topics = {}


def publish(topic: str, event: dict[str, Any]) -> None:
    """Publish ``event`` to every queue currently subscribed to ``topic``.

    Safe to call from any thread. A no-op if the loop hasn't been installed
    yet (e.g. tests with ``VOITTA_DISABLE_BACKGROUND=true``).
    """
    loop = _loop
    if loop is None or loop.is_closed():
        return
    with _lock:
        queues = list(_topics.get(topic, ()))
    if not queues:
        return
    for q in queues:
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except RuntimeError:
            # Loop closed between the check and the call. Drop quietly.
            return


class Subscription:
    """Per-connection inbox bound to a set of topics."""

    def __init__(self, topics: Iterable[str]) -> None:
        self.topics = tuple(topics)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)

    def attach(self) -> None:
        with _lock:
            for t in self.topics:
                _topics.setdefault(t, set()).add(self.queue)

    def detach(self) -> None:
        with _lock:
            for t in self.topics:
                qs = _topics.get(t)
                if qs and self.queue in qs:
                    qs.discard(self.queue)
                    if not qs:
                        _topics.pop(t, None)

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self.queue.get()


@asynccontextmanager
async def subscribe(topics: Iterable[str]) -> AsyncIterator[Subscription]:
    """Subscribe to ``topics`` for the duration of the ``async with`` block."""
    sub = Subscription(topics)
    sub.attach()
    try:
        yield sub
    finally:
        sub.detach()


def topic_subscriber_count(topic: str) -> int:
    """Test helper. How many queues are listening on ``topic``."""
    with _lock:
        return len(_topics.get(topic, ()))
