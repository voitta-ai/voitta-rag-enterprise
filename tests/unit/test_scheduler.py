"""Unit tests for the auto-sync scheduler tick.

The loop driver itself is just ``while True: tick(); sleep(60)`` — the
interesting logic is which rows ``_tick_once`` decides to enqueue. Each
test seeds folder_sync_sources rows in different states and asserts
which sync jobs land in the queue.
"""

from __future__ import annotations

import time

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import Folder, FolderSyncSource, Job
from voitta_image_rag.services.scheduler import _tick_once


def _make_folder(s, name: str) -> int:
    f = Folder(path=f"/tmp/{name}", display_name=name, enabled=True)
    s.add(f)
    s.flush()
    return f.id


def _queued_sync_folder_ids() -> list[int]:
    """Read every queued ``sync`` job's folder_id, in insertion order."""
    import json as _json

    with session_scope() as s:
        rows = s.query(Job).filter_by(kind="sync", state="queued").order_by(Job.id).all()
        return [_json.loads(r.payload)["folder_id"] for r in rows]


def test_disabled_rows_are_skipped(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "off")
        s.add(
            FolderSyncSource(
                folder_id=fid,
                source_type="github",
                gh_repo="https://example/repo",
                auto_sync_enabled=False,
                auto_sync_hours=1,
                last_synced_at=0,
            )
        )

    _tick_once()
    assert _queued_sync_folder_ids() == []


def test_enabled_overdue_row_is_enqueued(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "due")
        # last_synced_at deep in the past → due regardless of interval.
        s.add(
            FolderSyncSource(
                folder_id=fid,
                source_type="github",
                gh_repo="https://example/repo",
                auto_sync_enabled=True,
                auto_sync_hours=1,
                last_synced_at=0,
            )
        )

    _tick_once()
    assert _queued_sync_folder_ids() == [fid]


def test_not_yet_due_row_is_skipped(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "fresh")
        # Synced 10 minutes ago, interval=1h → not due for ~50 more min.
        s.add(
            FolderSyncSource(
                folder_id=fid,
                source_type="github",
                gh_repo="https://example/repo",
                auto_sync_enabled=True,
                auto_sync_hours=1,
                last_synced_at=int(time.time()) - 600,
            )
        )

    _tick_once()
    assert _queued_sync_folder_ids() == []


def test_first_run_with_null_last_synced_fires_immediately(env: None) -> None:
    """A freshly enabled row has last_synced_at=NULL — treat that as
    'never synced' and fire on the very next tick. This is what the
    user expects when toggling auto-sync on for the first time."""
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "fresh-toggle")
        s.add(
            FolderSyncSource(
                folder_id=fid,
                source_type="github",
                gh_repo="https://example/repo",
                auto_sync_enabled=True,
                auto_sync_hours=24,
                last_synced_at=None,
            )
        )

    _tick_once()
    assert _queued_sync_folder_ids() == [fid]


def test_drive_row_without_folders_is_skipped(env: None) -> None:
    """Mirror of the manual-trigger guard: a Drive row without picked
    folders would error every tick; skip it instead."""
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "gd-empty")
        s.add(
            FolderSyncSource(
                folder_id=fid,
                source_type="google_drive",
                gd_folder_id=None,
                auto_sync_enabled=True,
                auto_sync_hours=1,
                last_synced_at=0,
            )
        )

    _tick_once()
    assert _queued_sync_folder_ids() == []


def test_dedup_coalesces_back_to_back_ticks(env: None) -> None:
    """Two ticks against the same overdue row produce one queued job
    — the dedup_key (`sync:<folder_id>`) is honoured by enqueue."""
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "dedup")
        s.add(
            FolderSyncSource(
                folder_id=fid,
                source_type="github",
                gh_repo="https://example/repo",
                auto_sync_enabled=True,
                auto_sync_hours=1,
                last_synced_at=0,
            )
        )

    _tick_once()
    _tick_once()
    assert _queued_sync_folder_ids() == [fid]
