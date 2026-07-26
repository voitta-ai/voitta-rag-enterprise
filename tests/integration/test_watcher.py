"""Integration tests for the filesystem watcher."""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder, Job
from voitta_rag_enterprise.services.ignore import IgnoreMatcher
from voitta_rag_enterprise.services.watcher import WatcherManager


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

    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_DISABLE_BACKGROUND", "false")
    reset_settings_cache()

    src = tmp_path / "live"
    src.mkdir()

    from voitta_rag_enterprise.main import create_app

    from ..conftest import auth_as

    app = create_app()
    auth_as(app, "alice@x.com")
    with TestClient(app) as client:
        r = client.post("/api/folders", json={"name": src.name})
        assert r.status_code == 201
        (src / "fresh.txt").write_text("hi")
        jobs = _wait_until(lambda: _extract_jobs() if _extract_jobs() else None, timeout=4.0)

    assert jobs is not None and len(jobs) >= 1


def test_managed_folders_share_one_root_watch(env: None, tmp_path: Path) -> None:
    """Folders under VOITTA_ROOT_PATH route through ONE shared watch.

    Regression for the inotify-instance exhaustion: 50 folders used to
    mean 50 kernel instances (one emitter per scheduled watch); a second
    app instance on the same box then died with EMFILE at boot.
    """
    init_db()
    a = tmp_path / "folder_a"
    b = tmp_path / "folder_b"
    a.mkdir()
    b.mkdir()
    id_a, id_b = _make_folder(a), _make_folder(b)

    mgr = WatcherManager(debounce_s=0.1)
    with session_scope() as s:
        mgr.watch(s.get(Folder, id_a), max_file_bytes=10**9, ignore=IgnoreMatcher([]))
        mgr.watch(s.get(Folder, id_b), max_file_bytes=10**9, ignore=IgnoreMatcher([]))

    # Both managed → zero individual watches, one shared root watch.
    assert mgr._watches == {}
    assert set(mgr._managed_dirname) == {id_a, id_b}
    assert mgr._root_watch is not None
    # Exactly one scheduled emitter on the observer.
    assert len(mgr._observer._watches) == 1

    mgr.start()
    try:
        (a / "one.txt").write_text("in a")
        (b / "two.txt").write_text("in b")
        rows = _wait_until(
            lambda: (
                r
                if len(
                    r := [
                        (f.folder_id, f.rel_path)
                        for f in _all_files()
                    ]
                )
                == 2
                else None
            ),
            timeout=4.0,
        )
        # Unwatch A while running: pure map removal — the observer keeps
        # its single root watch (no emitter churn).
        mgr.unwatch(id_a)
        assert id_a not in mgr._managed_dirname
        assert len(mgr._observer._watches) == 1
    finally:
        mgr.stop()

    assert rows is not None and set(rows) == {(id_a, "one.txt"), (id_b, "two.txt")}


def _all_files() -> list[File]:
    with session_scope() as s:
        return list(s.execute(select(File)).scalars().all())


def test_folder_outside_root_gets_individual_watch(
    env: None, tmp_path: Path
) -> None:
    """Paths not directly under VOITTA_ROOT_PATH keep their own watch
    (desktop cloud-mounts, legacy external rows)."""
    init_db()
    outside = tmp_path / "nested" / "elsewhere"
    outside.mkdir(parents=True)
    fid = _make_folder(outside)

    mgr = WatcherManager(debounce_s=0.1)
    with session_scope() as s:
        mgr.watch(s.get(Folder, fid), max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    assert fid in mgr._watches
    assert fid not in mgr._managed_dirname
    mgr.unwatch(fid)
    assert mgr._watches == {}


def test_cross_folder_move_routes_to_both_handlers(
    env: None, tmp_path: Path
) -> None:
    """A move between two managed folders marks the source file deleted and
    upserts the destination — via the single root dispatcher."""
    import time as _time

    from watchdog.events import FileMovedEvent

    init_db()
    a = tmp_path / "src_a"
    b = tmp_path / "dst_b"
    a.mkdir()
    b.mkdir()
    id_a, id_b = _make_folder(a), _make_folder(b)
    # Pre-register the source file; physically place the destination file
    # so the upsert half finds real bytes.
    with session_scope() as s:
        s.add(
            File(
                folder_id=id_a,
                rel_path="doc.txt",
                last_seen_at=int(_time.time()),
                state="indexed",
            )
        )
    (b / "doc.txt").write_text("moved bytes")

    mgr = WatcherManager(debounce_s=0.01)
    with session_scope() as s:
        mgr.watch(s.get(Folder, id_a), max_file_bytes=10**9, ignore=IgnoreMatcher([]))
        mgr.watch(s.get(Folder, id_b), max_file_bytes=10**9, ignore=IgnoreMatcher([]))

    # Drive the dispatcher directly — no observer thread, fully deterministic
    # across platforms (the routing logic is what's under test).
    dispatcher = mgr._observer._handlers[mgr._root_watch].copy().pop()
    dispatcher.dispatch(FileMovedEvent(str(a / "doc.txt"), str(b / "doc.txt")))

    def _settled():
        rows = {(f.folder_id, f.rel_path): f.state for f in _all_files()}
        src_deleted = rows.get((id_a, "doc.txt")) == "deleted"
        dst_present = (id_b, "doc.txt") in rows
        return (rows if (src_deleted and dst_present) else None)

    rows = _wait_until(_settled, timeout=3.0)
    mgr.stop()
    assert rows is not None, f"move not routed: {_all_files()}"
    assert rows[(id_a, "doc.txt")] == "deleted"
    assert rows[(id_b, "doc.txt")] == "pending"


def test_stop_is_safe_after_failed_start(env: None, tmp_path: Path) -> None:
    """stop() must clean up even when start() raised mid-way (EMFILE case) —
    the old flag-gated stop() silently leaked the half-started observer."""
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    fid = _make_folder(root)
    mgr = WatcherManager(debounce_s=0.1)
    with session_scope() as s:
        mgr.watch(s.get(Folder, fid), max_file_bytes=10**9, ignore=IgnoreMatcher([]))

    def _boom() -> None:
        raise OSError(24, "inotify instance limit reached")

    mgr._observer.start = _boom  # type: ignore[method-assign]
    try:
        mgr.start()
        raise AssertionError("start() should have raised")
    except OSError:
        pass
    # Must not raise, and must be idempotent.
    mgr.stop()
    mgr.stop()


def test_recovery_sync_survives_watcher_failure(
    env: None, monkeypatch, tmp_path: Path
) -> None:
    """A watcher that cannot start degrades to watcher=None — the rest of
    the recovery chain (and therefore workers) still proceeds."""
    init_db()
    from voitta_rag_enterprise import main as main_mod
    from voitta_rag_enterprise.services import watcher as watcher_mod

    def _boom() -> None:
        raise OSError(24, "inotify instance limit reached")

    monkeypatch.setattr(watcher_mod, "from_settings_for_all_folders", _boom)
    phases: list[str] = []
    watcher = main_mod._recovery_sync(phases.append)
    assert watcher is None
    # The chain ran to the end — the scan phase comes after the watcher.
    assert "scanning folders" in phases


def test_health_reports_watcher_flag(client) -> None:
    """/api/health carries ``watcher`` so the SPA can flag degraded mode.
    disable_background mode (tests) runs no watcher by design → False."""
    h = client.get("/api/health").json()
    assert h["ready"] is True
    assert h["watcher"] is False


def _card_jobs() -> list[Job]:
    with session_scope() as s:
        return list(
            s.execute(
                select(Job).where(Job.kind == "rebuild_folder_cards")
            ).scalars().all()
        )


def test_new_subdir_file_triggers_card_rebuild(env: None, tmp_path: Path) -> None:
    """A file landing in a subdirectory via the WATCHER (rsync/scp/NFS-style
    drop — no upload endpoint involved) must refresh the folder's search
    cards, or the new subdir's name stays unsearchable until next restart."""
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)

    mgr = WatcherManager(debounce_s=0.05)
    with session_scope() as s:
        mgr.watch(s.get(Folder, folder_id), max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    mgr.start()
    try:
        (root / "reports" ).mkdir()
        (root / "reports" / "q3.txt").write_text("numbers")
        jobs = _wait_until(lambda: _card_jobs() or None, timeout=4.0)
    finally:
        mgr.stop()

    assert jobs is not None and len(jobs) == 1
    assert jobs[0].dedup_key == f"folder_cards:{folder_id}"

    # A MODIFY of the same (now-known) file must not enqueue another
    # rebuild — only new rows in subdirs do.
    mgr2 = WatcherManager(debounce_s=0.05)
    with session_scope() as s:
        mgr2.watch(s.get(Folder, folder_id), max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    mgr2.start()
    try:
        (root / "reports" / "q3.txt").write_text("updated numbers")
        _wait_until(lambda: len(_extract_jobs()) >= 1 or None, timeout=3.0)
        time.sleep(0.3)  # grace for any (wrong) extra card job
    finally:
        mgr2.stop()
    assert len(_card_jobs()) == 1  # still just the original


def test_root_level_file_does_not_trigger_card_rebuild(
    env: None, tmp_path: Path
) -> None:
    """A file at the folder root changes no subdir set — no rebuild job."""
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)

    mgr = WatcherManager(debounce_s=0.05)
    with session_scope() as s:
        mgr.watch(s.get(Folder, folder_id), max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    mgr.start()
    try:
        (root / "plain.txt").write_text("top-level")
        jobs = _wait_until(lambda: _extract_jobs() or None, timeout=3.0)
        time.sleep(0.3)
    finally:
        mgr.stop()
    assert jobs is not None
    assert _card_jobs() == []


def test_subdir_file_delete_triggers_card_rebuild(env: None, tmp_path: Path) -> None:
    """Deleting a subdir's file via the watcher refreshes cards too, so an
    emptied subdirectory sheds its card."""
    init_db()
    root = tmp_path / "src"
    root.mkdir()
    folder_id = _make_folder(root)
    sub = root / "archive"
    sub.mkdir()
    target = sub / "old.txt"
    target.write_text("bytes")
    with session_scope() as s:
        s.add(
            File(
                folder_id=folder_id,
                rel_path="archive/old.txt",
                last_seen_at=int(time.time()),
                state="indexed",
            )
        )

    mgr = WatcherManager(debounce_s=0.05)
    with session_scope() as s:
        mgr.watch(s.get(Folder, folder_id), max_file_bytes=10**9, ignore=IgnoreMatcher([]))
    mgr.start()
    try:
        target.unlink()
        jobs = _wait_until(lambda: _card_jobs() or None, timeout=4.0)
    finally:
        mgr.stop()
    assert jobs is not None and len(jobs) == 1


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
