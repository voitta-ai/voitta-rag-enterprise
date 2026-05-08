"""Filesystem watcher (watchdog) → enqueues extract / delete_file jobs."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from ..config import get_settings
from ..db.database import session_scope
from ..db.models import File, Folder
from . import job_queue
from .ignore import IgnoreMatcher
from .ignore import from_settings as _ignore_from_settings

logger = logging.getLogger(__name__)


class _Debouncer:
    """Coalesce repeated calls for the same key within ``delay`` seconds."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def schedule(self, key: str, fn: Callable[[], None]) -> None:
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self._delay, fn)
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def cancel_all(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()


class _FolderHandler(FileSystemEventHandler):
    def __init__(
        self,
        folder_id: int,
        folder_root: Path,
        ignore: IgnoreMatcher,
        max_file_bytes: int,
        debouncer: _Debouncer,
    ) -> None:
        self.folder_id = folder_id
        self.folder_root = folder_root
        self.ignore = ignore
        self.max_file_bytes = max_file_bytes
        self.debouncer = debouncer

    def _rel(self, path: str) -> str | None:
        try:
            rel = Path(path).resolve().relative_to(self.folder_root.resolve()).as_posix()
        except (OSError, ValueError):
            return None
        if self.ignore.matches(rel):
            return None
        return rel

    def _key(self, rel: str) -> str:
        return f"{self.folder_id}:{rel}"

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is not None:
            self.debouncer.schedule(self._key(rel), lambda: self._upsert_and_enqueue(rel))

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is not None:
            self.debouncer.schedule(self._key(rel), lambda: self._upsert_and_enqueue(rel))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is not None:
            self.debouncer.schedule(self._key(rel), lambda: self._mark_deleted(rel))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = self._rel(event.src_path)
        dst = self._rel(getattr(event, "dest_path", ""))
        if src is not None:
            self.debouncer.schedule(self._key(src), lambda: self._mark_deleted(src))
        if dst is not None:
            self.debouncer.schedule(self._key(dst), lambda: self._upsert_and_enqueue(dst))

    def _upsert_and_enqueue(self, rel: str) -> None:
        abs_path = self.folder_root / rel
        if not abs_path.exists() or not abs_path.is_file():
            return
        try:
            stat = abs_path.stat()
        except OSError:
            return
        if stat.st_size > self.max_file_bytes:
            return
        file_id: int | None = None
        with session_scope() as s:
            file = s.execute(
                select(File).where(File.folder_id == self.folder_id, File.rel_path == rel)
            ).scalar_one_or_none()
            now = int(time.time())
            if file is None:
                file = File(
                    folder_id=self.folder_id,
                    rel_path=rel,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    last_seen_at=now,
                    state="pending",
                )
                s.add(file)
                s.flush()
            else:
                file.size_bytes = stat.st_size
                file.mtime_ns = stat.st_mtime_ns
                file.last_seen_at = now
                if file.state == "deleted":
                    file.state = "pending"
            file_id = file.id
            job_queue.enqueue(
                s, "extract", {"file_id": file.id}, dedup_key=f"extract:{file.id}"
            )
        # Outside the session: tell the SPA the row exists *now*, in
        # ``state='pending'``. Without this the file stays invisible to
        # the UI until the worker finishes extracting and emits its own
        # event — which can be many minutes for a multi-PDF batch
        # behind ``_EXTRACT_LOCK``. The result was the user complaint:
        # uploaded files don't show up in counters / file list.
        if file_id is not None:
            from .indexing import publish_file_upserted

            publish_file_upserted(file_id)

    def _mark_deleted(self, rel: str) -> None:
        # We deliberately don't publish file.upserted here — emitting an
        # 'upsert' with state='deleted' would briefly flash a "deleted"
        # row in the SPA that is then removed by the worker's eventual
        # ``file.deleted`` event. Skipping keeps the UI flicker-free;
        # the worker's terminal event is the only one the SPA acts on
        # for deletes.
        with session_scope() as s:
            file = s.execute(
                select(File).where(File.folder_id == self.folder_id, File.rel_path == rel)
            ).scalar_one_or_none()
            if file is None or file.state == "deleted":
                return
            file.state = "deleted"
            job_queue.enqueue(
                s, "delete_file", {"file_id": file.id}, dedup_key=f"delete:{file.id}"
            )


class WatcherManager:
    def __init__(self, debounce_s: float = 0.5) -> None:
        self._observer: BaseObserver = Observer()
        self._debouncer = _Debouncer(debounce_s)
        self._watches: dict[int, object] = {}
        self._started = False

    def watch(self, folder: Folder, max_file_bytes: int, ignore: IgnoreMatcher) -> None:
        if folder.id in self._watches:
            return
        root = Path(folder.path)
        if not root.exists():
            logger.warning("watcher: folder path missing: %s", folder.path)
            return
        handler = _FolderHandler(folder.id, root, ignore, max_file_bytes, self._debouncer)
        watch = self._observer.schedule(handler, str(root), recursive=True)
        self._watches[folder.id] = watch

    def unwatch(self, folder_id: int) -> None:
        watch = self._watches.pop(folder_id, None)
        if watch is not None:
            self._observer.unschedule(watch)

    def start(self) -> None:
        if self._started:
            return
        self._observer.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._debouncer.cancel_all()
        self._started = False


def from_settings_for_all_folders() -> WatcherManager:
    """Build a manager with watches on every enabled folder."""
    settings = get_settings()
    ignore = _ignore_from_settings()
    mgr = WatcherManager()
    with session_scope() as s:
        for f in s.execute(select(Folder).where(Folder.enabled.is_(True))).scalars().all():
            mgr.watch(f, settings.max_file_bytes, ignore)
    return mgr


# Process-wide manager so route handlers can keep the watcher in sync with the
# folders table without reaching into ``app.state``. The lifespan in main.py
# installs/uninstalls it.
_default_manager: WatcherManager | None = None


def install_default(mgr: WatcherManager) -> None:
    global _default_manager
    _default_manager = mgr


def uninstall_default() -> None:
    global _default_manager
    _default_manager = None


def default_manager() -> WatcherManager | None:
    return _default_manager


def watch_folder_in_default(folder: Folder) -> None:
    """Add a folder to the running watcher (no-op if no manager is installed)."""
    mgr = _default_manager
    if mgr is None:
        return
    settings = get_settings()
    mgr.watch(folder, settings.max_file_bytes, _ignore_from_settings())


def unwatch_folder_in_default(folder_id: int) -> None:
    mgr = _default_manager
    if mgr is None:
        return
    mgr.unwatch(folder_id)
