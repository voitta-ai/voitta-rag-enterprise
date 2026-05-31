"""In-memory tracker for "which folders currently have queued/running jobs".

The SPA renders an ``indexing`` (blue, animated) pill on every folder
whose subtree has work in flight. Previously this was derived from the
SPA's local ``jobs`` store, which is capped at the most recent 50 entries
in ``static/js/ws.js`` — on a deep queue (a bulk reindex enqueues
thousands of extracts) the vast majority of folders never appeared in
that window, so the SPA showed a stale ``pending`` / ``indexed`` label
even though work was queued.

We fix this by maintaining a small ref-counted set here on the server:

* ``on_enqueued(folder_id)`` is called by ``job_queue.enqueue`` and the
  cancel/retry/sync REST handlers whenever a job transitions *into* the
  queued/running set.
* ``on_finished(folder_id)`` is called by ``job_queue.mark_done`` /
  ``mark_error`` and the cancel handlers whenever a job leaves that set.

Crossing the 0↔1 boundary publishes a ``folder.active_changed`` event
which the SPA's ``activeFolders`` store consumes. The event is coalesced
per folder_id in ``services/events.py``, so a burst of toggles for the
same folder during a reindex collapses to one delivered event.

The counter is in-process. It's bootstrapped from the DB on startup
(after ``reclaim_abandoned_jobs`` has settled the queue) and not
persisted; on a process restart we re-derive it from the surviving
queued rows.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import text

from . import events

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# folder_id -> number of currently-queued-or-running jobs targeting it.
# Modified under ``_lock``; readers use ``get_active_ids`` / ``is_active``
# which copy under the lock so callers never see a torn snapshot.
_counts: dict[int, int] = defaultdict(int)
_lock = threading.Lock()


def _publish(folder_id: int, active: bool) -> None:
    events.publish(
        "folders",
        {"type": "folder.active_changed", "folder_id": folder_id, "active": active},
    )


def folder_id_for_payload(session: "Session", payload: dict | None) -> int | None:
    """Resolve which folder a job payload targets.

    Folder-scoped jobs (``reindex_folder``, ``sync``) ship ``folder_id``
    directly. File-scoped jobs (``extract``, ``embed_text``,
    ``embed_image``, ``delete_file``) ship ``file_id`` only — we look up
    its folder in the files table within the caller's session so the
    lookup participates in the same transaction.

    Returns ``None`` for malformed payloads or unknown file ids; callers
    must tolerate that (e.g. legacy rows during a schema migration).
    """
    if not isinstance(payload, dict):
        return None
    fid = payload.get("folder_id")
    if isinstance(fid, int):
        return fid
    file_id = payload.get("file_id")
    if isinstance(file_id, int):
        row = session.execute(
            text("SELECT folder_id FROM files WHERE id = :id"), {"id": file_id}
        ).first()
        return int(row[0]) if row else None
    return None


def on_enqueued(folder_id: int | None) -> None:
    """Bump the count for ``folder_id``; publish ``active=True`` on 0→1."""
    if folder_id is None:
        return
    with _lock:
        was = _counts.get(folder_id, 0)
        _counts[folder_id] = was + 1
        crossed = was == 0
    if crossed:
        _publish(folder_id, True)


def on_finished(folder_id: int | None) -> None:
    """Decrement the count for ``folder_id``; publish ``active=False`` on 1→0.

    Defensive against under-counting (e.g. a finish event landing without
    a paired enqueue after a process restart that missed the bootstrap):
    the count is clamped at 0 and a redundant event is not published.
    """
    if folder_id is None:
        return
    with _lock:
        was = _counts.get(folder_id, 0)
        if was <= 0:
            _counts.pop(folder_id, None)
            return
        if was == 1:
            _counts.pop(folder_id, None)
            crossed = True
        else:
            _counts[folder_id] = was - 1
            crossed = False
    if crossed:
        _publish(folder_id, False)


def is_active(folder_id: int) -> bool:
    """Fast read for embedding into folder-stats payloads."""
    with _lock:
        return _counts.get(folder_id, 0) > 0


def get_active_ids() -> list[int]:
    """Snapshot of all currently-active folder ids. Used by the bootstrap
    REST endpoint so a freshly-loaded SPA can seed its set in one round
    trip rather than waiting for incremental events."""
    with _lock:
        return sorted(_counts.keys())


def init_from_db() -> None:
    """Populate ``_counts`` from the DB at process startup.

    Runs *after* ``reclaim_abandoned_jobs`` has flipped any leftover
    ``running`` rows to ``error`` so only legitimately queued/running
    work is counted. No events are published — there are no subscribers
    yet at lifespan-startup time, and the REST bootstrap endpoint reads
    the same in-memory snapshot we just built.
    """
    from ..db.database import session_scope

    folder_jobs: dict[int, int] = defaultdict(int)
    with session_scope() as session:
        # Folder-scoped jobs: reindex_folder, sync.
        rows = session.execute(
            text(
                "SELECT json_extract(payload, '$.folder_id') AS fid, COUNT(*) "
                "FROM jobs "
                "WHERE state IN ('queued','running') "
                "  AND json_extract(payload, '$.folder_id') IS NOT NULL "
                "GROUP BY fid"
            )
        ).all()
        for fid, n in rows:
            if fid is not None:
                folder_jobs[int(fid)] += int(n)

        # File-scoped jobs: extract, embed_text, embed_image, delete_file.
        # JOIN against files to resolve folder_id; CAST because
        # json_extract returns TEXT for the embedded number.
        rows = session.execute(
            text(
                "SELECT f.folder_id, COUNT(*) "
                "FROM jobs j "
                "JOIN files f "
                "  ON CAST(json_extract(j.payload, '$.file_id') AS INTEGER) = f.id "
                "WHERE j.state IN ('queued','running') "
                "  AND json_extract(j.payload, '$.file_id') IS NOT NULL "
                "GROUP BY f.folder_id"
            )
        ).all()
        for fid, n in rows:
            if fid is not None:
                folder_jobs[int(fid)] += int(n)

    with _lock:
        _counts.clear()
        _counts.update(folder_jobs)
    logger.info(
        "folder_active: bootstrap counted %d active folder(s): %s",
        len(folder_jobs),
        sorted(folder_jobs.keys()),
    )
