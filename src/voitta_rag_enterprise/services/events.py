"""In-process event broker for WebSocket fan-out.

Publishers call :func:`publish` from any thread; the broker schedules a
non-blocking put on each subscribed :class:`Subscription` using the
lifespan-installed event loop. WebSocket handlers create a Subscription and
await events through it.

Topics in use:
- ``folders``  — ``folder.added``, ``folder.upserted``, ``folder.removed``
- ``files``    — ``file.upserted``, ``file.deleted``
- ``jobs``     — ``job.started``, ``job.finished``
- ``stats``    — periodic counters

Coalescing
----------
Some event types (``file.upserted``, ``job.started``, ``job.finished``,
``folder.upserted``) are state snapshots, not point-in-time deltas — only
the *latest* version of the snapshot for a given key matters to the UI. Each
Subscription keys those events by ``(type, id)`` and replaces the prior
queued copy in-place when a new one arrives. A 30-event burst for one file
collapses to one queue entry; the WS pump dequeues fewer events and the UI
re-renders fewer times.

Discrete events (``file.deleted``, ``folder.added``, ``folder.removed``)
are appended without coalescing — each one matters.

Backpressure
------------
Each subscription has a soft cap on queued events. When a publish would
exceed it, the *oldest* coalescable event is dropped (rather than the
newest, as the previous bounded asyncio.Queue did) — losing a stale
snapshot is fine; losing a fresh one is what made the UI look frozen.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

_subs: dict[str, set[Subscription]] = {}
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()

# Per-subscription soft cap. With coalescing the realistic working size is
# much smaller (one entry per active file_id / job_id), so this is generous;
# at ~250 bytes per event JSON it caps at a few MB of resident state.
DEFAULT_CAPACITY = 16384

# Event types whose payload identifies a single resource — only the latest
# version of a given (type, key) is interesting. Tuple of (type, key_field).
_COALESCE_KEYS: dict[str, str] = {
    "file.upserted": "file_id_from_payload",  # special-cased below
    "job.started": "job_id",
    "job.finished": "job_id",
    "folder.upserted": "folder_id_from_payload",
    # Per-folder stats snapshots: only the latest matters. A burst of
    # commits on one folder during heavy indexing collapses to one
    # delivered event with the freshest counts.
    "folder.stats_changed": "folder_id",
    # Sync-source status snapshot (sync_status / sync_error /
    # last_synced_at). Emitted at each sync state transition; the modal
    # and sidebar only care about the latest, so coalesce by folder.
    "folder.sync_source_changed": "folder_id",
    # Boolean snapshot: does this folder have any queued/running job?
    # Maintained by ``services.folder_active``. Toggles can burst during
    # a reindex (one per file as work falls off the queue tail) so we
    # coalesce — only the latest active/inactive value matters.
    "folder.active_changed": "folder_id",
}


def _event_key(event: dict[str, Any]) -> tuple[str, Any] | None:
    """Return the dedup key for ``event`` or ``None`` if it must be appended.

    Files / folders are keyed by their nested id; jobs by the flat
    ``job_id`` field; ``folder.stats_changed`` by the flat ``folder_id``
    field. Anything not in :data:`_COALESCE_KEYS` returns None and is
    treated as a discrete event.
    """
    etype = event.get("type")
    if etype not in _COALESCE_KEYS:
        return None
    if etype == "file.upserted":
        fid = (event.get("file") or {}).get("id")
        return ("file.upserted", fid) if fid is not None else None
    if etype == "folder.upserted":
        fid = (event.get("folder") or {}).get("id")
        return ("folder.upserted", fid) if fid is not None else None
    if etype == "folder.stats_changed":
        fid = event.get("folder_id")
        return ("folder.stats_changed", fid) if fid is not None else None
    if etype == "folder.sync_source_changed":
        fid = event.get("folder_id")
        return ("folder.sync_source_changed", fid) if fid is not None else None
    if etype == "folder.active_changed":
        fid = event.get("folder_id")
        return ("folder.active_changed", fid) if fid is not None else None
    jid = event.get("job_id")
    return (etype, jid) if jid is not None else None


def install_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def uninstall_loop() -> None:
    global _loop, _subs
    _loop = None
    with _lock:
        _subs = {}


def publish(topic: str, event: dict[str, Any]) -> None:
    """Publish ``event`` to every subscription currently bound to ``topic``.

    Safe to call from any thread. No-op if the loop hasn't been installed
    yet (e.g. tests with ``VOITTA_DISABLE_BACKGROUND=true``).
    """
    loop = _loop
    if loop is None or loop.is_closed():
        return
    with _lock:
        subs = list(_subs.get(topic, ()))
    if not subs:
        return
    for sub in subs:
        try:
            loop.call_soon_threadsafe(sub.deliver, event)
        except RuntimeError:
            # Loop closed between the check and the call. Drop quietly.
            return


class Subscription:
    """Per-connection inbox bound to a set of topics.

    Implements coalescing on top of an unbounded internal store: writes are
    O(1) and replace any existing entry with the same dedup key. The WS
    pump awaits ``wait()`` and then drains via ``drain()``.
    """

    def __init__(self, topics: Iterable[str], capacity: int = DEFAULT_CAPACITY) -> None:
        self.topics = tuple(topics)
        # Ordered dicts give us O(1) replace-or-append plus FIFO eviction.
        # Coalesced events live keyed by (type, id); discrete events use a
        # synthetic monotonically-increasing key so they never collide.
        self._coalesced: OrderedDict[tuple[str, Any], dict[str, Any]] = OrderedDict()
        self._discrete: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._discrete_seq = 0
        self._capacity = capacity
        self._wakeup = asyncio.Event()
        self._dropped = 0
        # Cumulative counts so we can sanity-check coalescing in tests.
        self._published = 0
        self._delivered = 0

    # Called only on the loop thread (via ``call_soon_threadsafe``).
    def deliver(self, event: dict[str, Any]) -> None:
        self._published += 1
        key = _event_key(event)
        if key is not None:
            # Replace-in-place; move_to_end keeps newest at the back so the
            # WS pump drains in arrival order.
            self._coalesced[key] = event
            self._coalesced.move_to_end(key)
        else:
            self._discrete_seq += 1
            self._discrete[self._discrete_seq] = event
        self._evict_if_overflow()
        self._wakeup.set()

    def _evict_if_overflow(self) -> None:
        # Coalesced entries are usually <<< capacity; eviction here is the
        # safety valve when many distinct files churn faster than the WS
        # can drain. Drops the oldest coalesced entry (its update will be
        # superseded by the next file.upserted), then the oldest discrete
        # one if still over.
        while len(self._coalesced) + len(self._discrete) > self._capacity:
            if self._coalesced:
                self._coalesced.popitem(last=False)
            elif self._discrete:
                self._discrete.popitem(last=False)
            else:
                break
            self._dropped += 1

    def attach(self) -> None:
        with _lock:
            for t in self.topics:
                _subs.setdefault(t, set()).add(self)

    def detach(self) -> None:
        with _lock:
            for t in self.topics:
                qs = _subs.get(t)
                if qs and self in qs:
                    qs.discard(self)
                    if not qs:
                        _subs.pop(t, None)

    async def wait(self, timeout: float | None = None) -> bool:
        """Block until at least one event is queued, or ``timeout`` elapses.

        Returns ``True`` if events are available, ``False`` on timeout.
        """
        if self._coalesced or self._discrete:
            return True
        try:
            if timeout is None:
                await self._wakeup.wait()
            else:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
        except TimeoutError:
            return False
        finally:
            # Reset only when the buffer is empty so we don't miss an event
            # that landed between wait() returning and drain() running.
            if not (self._coalesced or self._discrete):
                self._wakeup.clear()
        return bool(self._coalesced or self._discrete)

    def drain(self, max_events: int = 256) -> list[dict[str, Any]]:
        """Pop up to ``max_events`` queued events in FIFO order.

        Coalesced and discrete buffers are interleaved by insertion order
        is approximated by draining all coalesced first then discrete —
        UI-wise this is fine since coalesced are state snapshots and
        discrete are mostly folder.added/removed which are independent.
        """
        out: list[dict[str, Any]] = []
        while self._coalesced and len(out) < max_events:
            _, ev = self._coalesced.popitem(last=False)
            out.append(ev)
        while self._discrete and len(out) < max_events:
            _, ev = self._discrete.popitem(last=False)
            out.append(ev)
        if not (self._coalesced or self._discrete):
            self._wakeup.clear()
        self._delivered += len(out)
        return out

    @property
    def stats(self) -> dict[str, int]:
        return {
            "published": self._published,
            "delivered": self._delivered,
            "dropped": self._dropped,
            "queued": len(self._coalesced) + len(self._discrete),
        }


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
    """Test helper. How many subscriptions are listening on ``topic``."""
    with _lock:
        return len(_subs.get(topic, ()))
