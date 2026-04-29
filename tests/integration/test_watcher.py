"""Integration tests for the filesystem watcher."""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import File, Folder, Job
from voitta_image_rag.services.ignore import IgnoreMatcher
from voitta_image_rag.services.watcher import WatcherManager


def _make_folder(path: Path) -> int:
    with session_scope() as s:
        f = Folder(path=str(path), display_name=path.name)
        s.add(f)
        s.flush()
        return f.id


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return predicate()


def _extract_jobs() -> list[Job]:
    with session_scope() as s:
        return list(
            s.execute(select(Job).where(Job.kind == "extract")).scalars().all()
        )


def _delete_jobs() -> list[Job]:
    with session_scope() as s:
        return list(
            s.execute(select(Job).where(Job.kind == "delete_file")).scalars().all()
        )


def test_creating_a_file_enqueues_extract(env: None, tmp_path: Path) -> None:
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)

    mgr = WatcherManager(debounce_s=0.1)
    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        mgr.watch(folder, max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    mgr.start()

    try:
        (root / "a.txt").write_text("hello")
        jobs = _wait_until(lambda: _extract_jobs() if _extract_jobs() else None)
    finally:
        mgr.stop()

    assert jobs is not None and len(jobs) == 1
    assert jobs[0].dedup_key.startswith("extract:")

    with session_scope() as s:
        files = list(s.execute(select(File)).scalars().all())
    assert [f.rel_path for f in files] == ["a.txt"]


def test_rapid_saves_coalesce_to_one_extract(env: None, tmp_path: Path) -> None:
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)
    target = root / "x.txt"
    target.write_text("seed")

    mgr = WatcherManager(debounce_s=0.3)
    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        mgr.watch(folder, max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    mgr.start()

    try:
        for i in range(10):
            target.write_text(f"v{i}")
            time.sleep(0.02)
        jobs = _wait_until(lambda: _extract_jobs() if _extract_jobs() else None, timeout=3.0)
    finally:
        mgr.stop()

    assert jobs is not None
    # Debounce + dedup_key collapse to a single in-flight extract.
    assert len(jobs) == 1


def test_deleting_a_file_enqueues_delete_file(env: None, tmp_path: Path) -> None:
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)
    target = root / "x.txt"
    target.write_text("seed")

    # Pre-register the file so the watcher's delete handler has something to mark.
    with session_scope() as s:
        s.add(
            File(
                folder_id=folder_id,
                rel_path="x.txt",
                size_bytes=4,
                mtime_ns=target.stat().st_mtime_ns,
                last_seen_at=int(time.time()),
                state="pending",
            )
        )

    mgr = WatcherManager(debounce_s=0.1)
    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        mgr.watch(folder, max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    mgr.start()

    try:
        target.unlink()
        jobs = _wait_until(lambda: _delete_jobs() if _delete_jobs() else None)
    finally:
        mgr.stop()

    assert jobs is not None and len(jobs) == 1
    with session_scope() as s:
        f = s.execute(select(File).where(File.rel_path == "x.txt")).scalar_one()
        assert f.state == "deleted"


def test_ignored_pattern_does_not_enqueue(env: None, tmp_path: Path) -> None:
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)

    mgr = WatcherManager(debounce_s=0.1)
    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        mgr.watch(folder, max_file_bytes=10**9, ignore=IgnoreMatcher([".git"]))
    mgr.start()

    try:
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref")
        time.sleep(0.4)  # debounce + small grace
    finally:
        mgr.stop()

    assert _extract_jobs() == []


def test_post_folder_registers_with_running_watcher(
    env: None, monkeypatch, tmp_path: Path
) -> None:
    """End-to-end: POST /api/folders → drop a file → watcher enqueues extract."""
    from fastapi.testclient import TestClient

    from voitta_image_rag.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_DISABLE_BACKGROUND", "false")
    reset_settings_cache()

    src = tmp_path / "live"
    src.mkdir()

    from voitta_image_rag.main import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/folders",
            json={"path": str(src)},
            headers={"X-Forwarded-Email": "alice@x.com"},
        )
        assert r.status_code == 201
        (src / "fresh.txt").write_text("hi")
        jobs = _wait_until(lambda: _extract_jobs() if _extract_jobs() else None, timeout=4.0)

    assert jobs is not None and len(jobs) >= 1


def test_oversize_file_is_skipped(env: None, tmp_path: Path) -> None:
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)

    mgr = WatcherManager(debounce_s=0.1)
    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        mgr.watch(folder, max_file_bytes=10, ignore=IgnoreMatcher([]))
    mgr.start()

    try:
        (root / "big.txt").write_text("x" * 1000)
        time.sleep(0.4)
    finally:
        mgr.stop()

    assert _extract_jobs() == []
