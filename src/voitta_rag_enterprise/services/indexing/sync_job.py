"""The ``sync`` job handler: pull a folder's remote onto disk and record status."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ...db.database import session_scope
from ...db.models import Folder
from ...logging_config import bind_context
from .. import events
from .common import _format_exception, logger


async def run_sync(payload: dict) -> dict | None:
    """Worker handler for ``sync`` jobs: pulls a folder's remote, mirrors files
    onto disk, and records status on the ``folder_sync_sources`` row.

    Side effects on disk drive the existing extract / delete pipeline through
    the watcher — this handler does not enqueue extracts itself.

    Returns the connector's stats dict (files_added, pages_written, errors, …)
    so the worker persists it on the job and the Jobs panel can show the detail.
    """
    folder_id = int(payload["folder_id"])
    with bind_context(folder_id=folder_id):
        try:
            stats = await _run_sync_inner(folder_id)
        except Exception:
            logger.exception("sync failed")
            _mark_sync_error(folder_id, _format_exception("sync failed"))
            raise
        # Reconcile the folder against the freshly-written ``.voitta_sources.json``
        # so the connector's per-file provenance (source_url / source_meta) lands
        # on the File rows — and gets enqueued for (re)extract where changed.
        # The watcher only sees byte changes and never reads the sidecar, so
        # without this an *unchanged* file's owner/date metadata would never
        # reach the DB (it'd wait for the next process restart's startup scan).
        await asyncio.to_thread(_rescan_after_sync, folder_id)
        return stats


def _rescan_after_sync(folder_id: int) -> None:
    """Run scan_folder so the post-sync sidecar updates File.source_url/_meta."""
    from ..scanner import scan_folder

    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        if folder is not None:
            scan_folder(s, folder)


def _publish_sync_source_changed(folder_id: int) -> None:
    """Push the current sync-source status to subscribed clients.

    The sidebar's badge already follows ``folder.sync_progress`` for the
    in-progress phases. This event covers the *terminal* fields the modal
    renders — ``sync_status`` / ``sync_error`` / ``last_synced_at`` — so an
    open modal updates the moment a job finishes (or errors, or starts in
    another tab), without needing the user to close + reopen it.
    """
    with session_scope() as s:
        from ...db.models import FolderSyncSource

        src = s.get(FolderSyncSource, folder_id)
        if src is None:
            return
        event = {
            "type": "folder.sync_source_changed",
            "folder_id": folder_id,
            "sync_status": src.sync_status,
            "sync_error": src.sync_error,
            "last_synced_at": src.last_synced_at,
        }
    events.publish("folders", event)


async def _run_sync_inner(folder_id: int) -> None:
    from ..sync import get_connector

    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        if folder is None:
            logger.info("sync abort: folder %d gone", folder_id)
            return
        from ...db.models import FolderSyncSource

        source = s.get(FolderSyncSource, folder_id)
        if source is None:
            raise RuntimeError(f"no sync source configured for folder {folder_id}")
        folder_path = folder.path
        source_type = source.source_type

        # Each connector builds its own kwargs from the row (the per-type
        # switchboard used to live here). Done while the session is open since
        # it reads ``source`` fields; we then release the session before the
        # slow network/disk work below.
        connector = get_connector(source_type)
        cfg: dict[str, object] = connector.resolve_config(source)

        source.sync_status = "syncing"
        source.sync_error = None

    _publish_sync_source_changed(folder_id)
    logger.info("sync begin folder=%s type=%s", folder_path, source_type)

    # Bridge the connector's progress callback into the WS event stream so
    # the SPA can show a live "Syncing — listing 1/3" / "Syncing — 47/200"
    # pill. Sync runs on a worker thread (via ``asyncio.to_thread`` inside
    # ``GoogleDriveConnector.sync``) but ``events.publish`` is thread-safe
    # — it round-trips through ``run_coroutine_threadsafe`` if the asyncio
    # loop has been wired in.
    def _on_progress(
        phase: str,
        done: int,
        total: int,
        detail: dict | None = None,
    ) -> None:
        event = {
            "type": "folder.sync_progress",
            "folder_id": folder_id,
            "phase": phase,
            "done": done,
            "total": total,
        }
        if detail:
            event["detail"] = detail
        events.publish("folders", event)

    if connector.supports_progress:
        cfg["progress_cb"] = _on_progress

    # Initial event from the worker side, before the connector starts —
    # gives the SPA something to render the moment the sync job runs,
    # rather than waiting for the connector to issue its first progress
    # call (which can be seconds later if Drive auth is slow).
    _on_progress("queued", 0, 0)

    # GitHub SSH/agent clones can block on a YubiKey touch; surface a banner via
    # the same WS channel so the user knows to tap. No-op for other connectors.
    def _git_touch(state: str) -> None:
        events.publish(
            "folders",
            {"type": "git.touch", "folder_id": folder_id, "state": state},
        )

    started = time.perf_counter()
    try:
        if source_type == "github":
            from ..sync.github import git_touch_scope

            with git_touch_scope(_git_touch):
                stats = await connector.sync(
                    folder_root=Path(folder_path), **cfg
                )
        else:
            stats = await connector.sync(
                folder_root=Path(folder_path),
                **cfg,
            )
    except Exception:
        with session_scope() as s2:
            from ...db.models import FolderSyncSource

            src = s2.get(FolderSyncSource, folder_id)
            if src is not None:
                src.sync_status = "error"
        _publish_sync_source_changed(folder_id)
        # Final event so the SPA's badge clears even on failure.
        _on_progress("done", 0, 0, None)
        raise

    elapsed = time.perf_counter() - started
    logger.info(
        "sync done folder=%s in %.1fs: %s", folder_path, elapsed, stats.as_dict()
    )

    with session_scope() as s3:
        from ...db.models import FolderSyncSource

        src = s3.get(FolderSyncSource, folder_id)
        if src is not None:
            src.sync_status = "error" if stats.errors else "idle"
            src.sync_error = "; ".join(stats.errors) if stats.errors else None
            src.last_synced_at = int(time.time())
            # Microsoft rotates refresh tokens on most refresh calls; the
            # connector parks the rotated value on its MicrosoftAuth and
            # we persist it here so the next sync can mint a token.
            if source_type in ("sharepoint", "teams"):
                ms_auth = cfg.get("auth")
                rotated = getattr(ms_auth, "rotated_refresh_token", None)
                if rotated:
                    src.ms_refresh_token = rotated

    _publish_sync_source_changed(folder_id)
    # Final clear-the-badge event. The GD connector already emits "done"
    # on success, but other connectors may not — this guarantees the SPA
    # drops the sync pill no matter which connector ran.
    _on_progress("done", 0, 0, None)

    # Returned to the worker → persisted on the job → shown in the Jobs panel
    # detail. ``elapsed_s`` is added so the row can show how long it took.
    return {**stats.as_dict(), "elapsed_s": round(elapsed, 1)}


def _mark_sync_error(folder_id: int, message: str) -> None:
    with session_scope() as s:
        from ...db.models import FolderSyncSource

        src = s.get(FolderSyncSource, folder_id)
        if src is None:
            return
        src.sync_status = "error"
        src.sync_error = message[:4000]
    _publish_sync_source_changed(folder_id)
