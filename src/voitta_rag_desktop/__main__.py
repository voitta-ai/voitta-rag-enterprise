"""Briefcase entry point for the Voitta RAG menu-bar .app.

Runs before any ``voitta_rag_enterprise`` import (that package needs the heavy
stack the installer fetches). Responsibilities:

  1. Exit immediately if we're a multiprocessing spawn child.
  2. Acquire a single-instance lock.
  3. Prepare the writable user-data dir under ~/Library/Application Support.
  4. Route lazy pip installs into ``userbase/`` via ``PIP_PREFIX`` and put it
     on ``sys.path`` so freshly-installed packages import.
  5. Export the env the server reads: single-user, managed Qdrant (binary in
     userbase/bin), data dir, root path, and the bundled SPA dir.
  6. Redirect stdout/stderr to a logfile (a frozen .app has no terminal).
  7. Hand off to the menu-bar shell.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
from pathlib import Path

# Must run at import time in a frozen exe: multiprocessing spawn children
# re-run this module and have to exit before touching Cocoa/UI.
multiprocessing.freeze_support()

_APP_SUPPORT_NAME = "Voitta RAG"
_ROOT_FILES_DIRNAME = "voitta-files"


def _is_mp_child() -> bool:
    if any("--multiprocessing-fork" in a for a in sys.argv):
        return True
    return bool(os.environ.get("MULTIPROCESSING_FORKING_DISABLE"))


def _user_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / _APP_SUPPORT_NAME


def _bundle_resources_dir() -> Path:
    """The ``resources`` subtree shipped inside the .app bundle."""
    try:
        import voitta_rag_desktop

        return Path(voitta_rag_desktop.__file__).resolve().parent / "resources"
    except Exception:
        rp = os.environ.get("RESOURCEPATH")
        if rp:
            return Path(rp)
        return Path(sys.executable).resolve().parent.parent / "Resources"


def _acquire_instance_lock() -> bool:
    """True if this is the only running instance (flock auto-releases on exit)."""
    import fcntl

    lock_path = _user_data_dir() / ".voitta.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Keep the fd alive on the function object for the process lifetime.
        _acquire_instance_lock._fd = lock_path.open("w")  # type: ignore[attr-defined]
        fcntl.flock(
            _acquire_instance_lock._fd,  # type: ignore[attr-defined]
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
        return True
    except (OSError, BlockingIOError):
        return False


def main() -> int:
    if _is_mp_child():
        return 0
    if not _acquire_instance_lock():
        return 0  # another instance already running

    # ---- SIGPIPE: ignore it (THE crash fix) --------------------------------
    # Python normally sets SIGPIPE to SIG_IGN at interpreter init, so a write to
    # a closed socket raises BrokenPipeError instead of killing the process. But
    # Cocoa/PyObjC's NSApplication run loop (started by rumps) resets SIGPIPE to
    # its default "terminate" disposition. After that, the uvicorn server thread
    # writing to a browser connection the SPA has half-closed gets SIGPIPE and
    # the WHOLE process dies — silently: faulthandler doesn't trap SIGPIPE and
    # macOS writes no crash report for it. Re-assert SIG_IGN here; the shell's
    # main-thread UI timer also re-asserts it for the first few ticks in case
    # Cocoa flips it back during NSApp startup.
    import signal as _signal

    try:
        _signal.signal(_signal.SIGPIPE, _signal.SIG_IGN)
    except (ValueError, OSError):
        pass

    user = _user_data_dir()
    user.mkdir(parents=True, exist_ok=True)
    res = _bundle_resources_dir()

    # Lazy pip installs land in userbase/ (prefix layout so pip sees the
    # already-bundled packages and skips them). Put its site-packages on the
    # path so freshly-installed deps import in this same process.
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    user_prefix = user / "userbase"
    user_site = user_prefix / "lib" / py_dir / "site-packages"
    user_site.mkdir(parents=True, exist_ok=True)
    os.environ["PIP_PREFIX"] = str(user_prefix)
    if str(user_site) not in sys.path:
        sys.path.insert(0, str(user_site))

    # Server environment — single-user, managed Qdrant (binary downloaded into
    # userbase/bin by the installer), writable data dir, a root for indexable
    # folders, and the SPA shipped in the bundle.
    root_files = user / _ROOT_FILES_DIRNAME
    root_files.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("VOITTA_SINGLE_USER", "true")
    os.environ.setdefault("VOITTA_DATA_DIR", str(user / "data"))
    os.environ.setdefault("VOITTA_ROOT_PATH", str(root_files))
    os.environ.setdefault("VOITTA_QDRANT_MODE", "managed")
    os.environ.setdefault("VOITTA_QDRANT_BINARY", str(user_prefix / "bin" / "qdrant"))
    static_dir = res / "static"
    if static_dir.is_dir():
        os.environ.setdefault("VOITTA_STATIC_DIR", str(static_dir))

    # Frozen .app has no terminal — capture output for Console.app / debugging.
    # APPEND (not truncate): a crash + relaunch must not wipe the prior session's
    # evidence. We delimit sessions with a banner and roll the file if it grows
    # large so it can't bloat unbounded.
    log_path = user / "voitta-rag.log"
    try:
        if log_path.is_file() and log_path.stat().st_size > 5_000_000:
            log_path.replace(log_path.with_suffix(".log.1"))
    except OSError:
        pass
    try:
        log_fp = log_path.open("a", buffering=1, encoding="utf-8")
        sys.stdout = log_fp
        sys.stderr = log_fp
    except OSError:
        log_fp = None

    if log_fp is not None:
        from ._version import __version__

        # Wall-clock banner so sessions are distinguishable in the appended log.
        from datetime import datetime

        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(
            f"\n===== Voitta RAG v{__version__} session start {stamp} "
            f"pid={os.getpid()} =====",
            flush=True,
        )

        # faulthandler dumps a C-level traceback of every thread on a fatal
        # signal (SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL) — the ONLY way to see a
        # hard PyObjC/native crash that produces no Python traceback. Also wire
        # SIGTERM so an external kill (or `Quit`) leaves a dump too.
        try:
            import faulthandler
            import signal

            faulthandler.enable(file=log_fp, all_threads=True)
            faulthandler.register(signal.SIGTERM, file=log_fp, all_threads=True, chain=True)
        except Exception:  # noqa: BLE001 — diagnostics are best-effort
            pass

        # Last-resort hooks: log any uncaught exception on the main thread and
        # in worker threads (daemon threads otherwise die silently).
        def _log_uncaught(exc_type, exc, tb):
            import traceback

            print("!!! UNCAUGHT EXCEPTION (main thread):", flush=True)
            traceback.print_exception(exc_type, exc, tb)

        sys.excepthook = _log_uncaught

        def _log_thread_uncaught(args):
            import traceback

            print(f"!!! UNCAUGHT EXCEPTION (thread {args.thread.name}):", flush=True)
            traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)

        threading.excepthook = _log_thread_uncaught

    from voitta_rag_desktop.shell import main as shell_main

    return shell_main(user_data_dir=user, resources_dir=res)


if __name__ == "__main__":
    sys.exit(main())
