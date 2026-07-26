"""Filesystem watcher (watchdog) → enqueues extract / delete_file jobs.

Watch topology: managed folders (direct children of ``VOITTA_ROOT_PATH``)
share ONE recursive watch on the root — events are routed to the owning
folder's handler by their first path segment. This keeps the kernel
inotify-instance cost at one per process instead of one per folder
(watchdog creates an inotify instance per scheduled watch; 50 folders
used to consume 50 of the default ``fs.inotify.max_user_instances=128``,
and a second app instance on the same box then failed to boot). Folders
living elsewhere (desktop cloud-mounts, legacy external paths) still get
an individual watch each.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
    """Coalesce repeated calls for the same key within ``delay`` seconds.

    Callbacks are dispatched through a bounded ThreadPoolExecutor so that a
    burst of filesystem events (e.g. a git sync landing hundreds of files at
    once) cannot exhaust the SQLAlchemy connection pool by spawning an
    unbounded number of concurrent threads.
    """

    def __init__(self, delay: float, max_workers: int = 4) -> None:
        self._delay = delay
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="watcher-db"
        )

    def schedule(self, key: str, fn: Callable[[], None]) -> None:
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
            # Timer fires a lightweight submit(); actual DB work runs inside
            # the bounded executor so at most max_workers sessions are open.
            timer = threading.Timer(self._delay, self._executor.submit, args=(fn,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def cancel_all(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
        self._executor.shutdown(wait=False)


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


class _RootDispatcher(FileSystemEventHandler):
    """Route events from the single root watch to the owning folder's handler.

    ``handlers`` is the manager's live ``dirname → _FolderHandler`` map —
    shared by reference, so folders added/removed at runtime take effect
    without touching the observer. A moved event whose src and dest fall in
    different folders is dispatched to both; each handler's own ``_rel``
    guard makes it act only on the half inside its folder root.
    """

    def __init__(self, root: Path, handlers: dict[str, _FolderHandler]) -> None:
        self._root = root
        self._handlers = handlers

    def _handler_for(self, path: str) -> _FolderHandler | None:
        if not path:
            return None
        try:
            rel = Path(path).resolve().relative_to(self._root)
        except (OSError, ValueError):
            return None
        return self._handlers.get(rel.parts[0]) if rel.parts else None

    def dispatch(self, event: FileSystemEvent) -> None:
        targets = []
        h = self._handler_for(event.src_path)
        if h is not None:
            targets.append(h)
        dest_h = self._handler_for(getattr(event, "dest_path", ""))
        if dest_h is not None and dest_h not in targets:
            targets.append(dest_h)
        for target in targets:
            target.dispatch(event)


class WatcherManager:
    def __init__(self, debounce_s: float = 0.5) -> None:
        self._observer: BaseObserver = Observer()
        self._debouncer = _Debouncer(debounce_s)
        # Individual watches for folders OUTSIDE the managed root, keyed by
        # folder_id. Managed folders never appear here — they route through
        # the shared root watch below.
        self._watches: dict[int, object] = {}
        # Shared root watch state. ``_root`` is the resolved
        # VOITTA_ROOT_PATH (None when unconfigured — then every folder
        # gets an individual watch, the pre-root-watch behaviour).
        self._root: Path | None = None
        root = get_settings().root_path
        if root is not None:
            with contextlib.suppress(OSError):
                self._root = Path(root).expanduser().resolve()
        self._root_handlers: dict[str, _FolderHandler] = {}
        self._managed_dirname: dict[int, str] = {}
        self._root_watch: object | None = None
        self._started = False

    def _is_managed(self, folder_root: Path) -> bool:
        """True when ``folder_root`` is a real direct child of the root.

        Symlinked children are excluded: a recursive inotify watch does not
        traverse symlinks, so routing them through the root watch would
        silently drop their events — they keep an individual watch instead.
        """
        if self._root is None:
            return False
        try:
            return (
                folder_root.parent.resolve() == self._root
                and not folder_root.is_symlink()
            )
        except OSError:
            return False

    def _ensure_root_watch(self) -> None:
        if self._root_watch is not None or self._root is None:
            return
        # The root may not exist yet on a fresh install (it's created on
        # first folder creation) — scheduling a watch on a missing dir
        # raises, so create it the same way create_folder would.
        self._root.mkdir(parents=True, exist_ok=True)
        dispatcher = _RootDispatcher(self._root, self._root_handlers)
        self._root_watch = self._observer.schedule(
            dispatcher, str(self._root), recursive=True
        )

    def watch(self, folder: Folder, max_file_bytes: int, ignore: IgnoreMatcher) -> None:
        if folder.id in self._managed_dirname or folder.id in self._watches:
            return
        root = Path(folder.path)
        if self._is_managed(root):
            # No exists() check: the map entry is inert until the directory
            # appears, and the root watch (already recursive) picks events
            # up the moment it does.
            handler = _FolderHandler(
                folder.id, root, ignore, max_file_bytes, self._debouncer
            )
            self._root_handlers[root.name] = handler
            self._managed_dirname[folder.id] = root.name
            self._ensure_root_watch()
            return
        if not root.exists():
            logger.warning("watcher: folder path missing: %s", folder.path)
            return
        handler = _FolderHandler(folder.id, root, ignore, max_file_bytes, self._debouncer)
        watch = self._observer.schedule(handler, str(root), recursive=True)
        self._watches[folder.id] = watch

    def unwatch(self, folder_id: int) -> None:
        dirname = self._managed_dirname.pop(folder_id, None)
        if dirname is not None:
            # Pure map removal — the shared root watch stays (one instance,
            # events for the removed subtree now fall through the map).
            self._root_handlers.pop(dirname, None)
            return
        watch = self._watches.pop(folder_id, None)
        if watch is not None:
            self._observer.unschedule(watch)

    def start(self) -> None:
        if self._started:
            return
        self._observer.start()
        self._started = True

    def stop(self) -> None:
        """Tear down at ANY lifecycle point — including after a failed
        ``start()``.

        watchdog's ``Observer.start()`` starts the observer thread first and
        each emitter after, so an OSError mid-way (e.g. the kernel's inotify
        instance limit) leaves the thread and some emitters alive. Gating
        this on ``_started`` (which is only set on full success) would leak
        them — so the teardown is unconditional and idempotent.
        """
        with contextlib.suppress(RuntimeError):
            self._observer.stop()
            if self._observer.is_alive():
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
    """Add a folder to the running watcher (no-op if no manager is installed).

    Never raises: ``observer.schedule`` on a live observer starts the
    emitter immediately, which can hit OS limits (inotify EMFILE) — that
    must degrade to "folder unwatched until next restart", not fail the
    folder-create/rename request that triggered it. Managed folders don't
    take this path's risky branch at all (map insert, no new emitter).
    """
    mgr = _default_manager
    if mgr is None:
        return
    settings = get_settings()
    try:
        mgr.watch(folder, settings.max_file_bytes, _ignore_from_settings())
    except Exception:
        logger.exception(
            "watcher: could not watch folder %d (%s) — changes in it will be "
            "picked up on next restart's scan",
            folder.id,
            folder.path,
        )


def unwatch_folder_in_default(folder_id: int) -> None:
    mgr = _default_manager
    if mgr is None:
        return
    try:
        mgr.unwatch(folder_id)
    except Exception:
        logger.exception("watcher: could not unwatch folder %d", folder_id)
