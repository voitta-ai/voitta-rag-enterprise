"""Periodic auto-sync scheduler.

When a ``folder_sync_sources`` row sets ``auto_sync_enabled=true``, this
loop enqueues a sync job every ``auto_sync_hours`` (1-24). The job goes
through the same queue + dedup as a manual ``/sync/trigger`` call, so a
sync still running from the previous tick (or from a manual click)
won't get a duplicate.

The interval is intentionally hour-grained and capped at 24h. Wider
schedules (days, weeks) belong to a real cron, not this in-process
loop — and finer ones (minutes) blow through Drive quota with no
visible benefit since the watcher already picks up newly-arrived files
once they land on disk.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from ..db.database import session_scope
from ..db.models import FolderSyncSource

logger = logging.getLogger(__name__)


# How often the scheduler wakes to evaluate due rows. One minute means a
# 1-hour interval fires within ~60s of the schedule, while staying
# essentially free (one tiny indexed SELECT per minute).
TICK_SECONDS = 60


async def run_forever() -> None:
    """Drive the scheduler until cancelled (lifespan shutdown)."""
    logger.info("auto-sync scheduler started (tick=%ds)", TICK_SECONDS)
    try:
        while True:
            try:
                _tick_once()
            except Exception:
                # A flaky DB / row should not kill the scheduler — log
                # and try again next tick. Per-row failures inside
                # ``_tick_once`` are already isolated.
                logger.exception("auto-sync tick failed; retrying next interval")
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("auto-sync scheduler stopped")
        raise


def _tick_once() -> None:
    """One scan of folder_sync_sources, enqueuing every due row.

    Pulled out so tests can step the loop deterministically without
    spinning ``run_forever``.
    """
    from . import job_queue

    now = int(time.time())
    enqueued: list[int] = []
    with session_scope() as s:
        rows = (
            s.execute(
                select(FolderSyncSource).where(
                    FolderSyncSource.auto_sync_enabled.is_(True)
                )
            )
            .scalars()
            .all()
        )
        for src in rows:
            hours = max(1, min(24, int(src.auto_sync_hours or 6)))
            due_at = (src.last_synced_at or 0) + hours * 3600
            if now < due_at:
                continue
            # Mirror the trigger endpoint's "no folders → no sync" guard.
            # A half-configured Drive row would otherwise produce an
            # error-marked sync_status every tick.
            if src.source_type == "google_drive" and not src.gd_folder_id:
                continue
            # SharePoint: skip if no sites picked AND "all sites" is off.
            if src.source_type == "sharepoint" and not (
                src.sp_all_sites or src.sp_selected_sites
            ):
                continue
            # Teams: skip if user_mode is "specific" but no user is set.
            if src.source_type == "teams" and (
                (src.tm_user_mode or "me") == "specific" and not src.tm_user_id
            ):
                continue
            # NFS: skip if neither subpaths (new) nor subpath (legacy) is set.
            if src.source_type == "nfs" and not (
                src.nfs_subpaths or src.nfs_subpath
            ):
                continue
            job_queue.enqueue(
                s,
                "sync",
                {"folder_id": src.folder_id},
                dedup_key=f"sync:{src.folder_id}",
            )
            enqueued.append(src.folder_id)
    if enqueued:
        logger.info("auto-sync: enqueued sync for folders %s", enqueued)
