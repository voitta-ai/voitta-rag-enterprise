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


def _ensure_userbase_on_path() -> None:
    """Put the lazy-install ``userbase/`` site-packages on ``sys.path`` and
    export it via ``PYTHONPATH`` + ``PIP_PREFIX``.

    The heavy stack (mineru, torch, …) is pip-installed at first launch into
    ``userbase/`` (prefix layout), NOT into the bundle. The main process adds it
    to ``sys.path`` at startup, but **subprocesses don't inherit sys.path** — so
    the MinerU parser daemon (and its spawn render workers) couldn't import
    mineru. Exporting ``PYTHONPATH`` makes every child/grandchild inherit it;
    inserting into ``sys.path`` covers the current process. Idempotent."""
    user = _user_data_dir()
    py_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    user_prefix = user / "userbase"
    user_site = user_prefix / "lib" / py_dir / "site-packages"
    try:
        user_site.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    os.environ["PIP_PREFIX"] = str(user_prefix)
    us = str(user_site)
    if us not in sys.path:
        sys.path.insert(0, us)
    # Prepend to PYTHONPATH so spawned children (e.g. MinerU's render pool)
    # inherit it without re-running the app's startup path logic.
    existing = os.environ.get("PYTHONPATH", "")
    parts = existing.split(os.pathsep) if existing else []
    if us not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([us, *parts]) if parts else us


def _maybe_run_as_interpreter() -> int | None:
    """Act as a plain Python interpreter when invoked with ``-m`` / ``-c``.

    The Briefcase stub always launches this module (ignoring ``-m``/``-c`` on
    the command line), and the bundle ships no standalone ``python`` binary —
    so ``sys.executable`` IS this app. Anything that does
    ``subprocess.Popen([sys.executable, "-m", …])`` or that uses the
    multiprocessing **spawn** start method (which launches
    ``sys.executable -c "from multiprocessing.spawn import spawn_main; …"``)
    would otherwise just relaunch the menu-bar app, hit the single-instance
    lock, and exit 0.

    That is exactly what broke PDF parsing: the MinerU parser runs as a
    ``-m`` daemon subprocess, and MinerU's page-render pool uses spawn. Both
    silently no-op'd, so the daemon's first read returned empty and every PDF
    was misreported as a 600s timeout. Routing ``-m``/``-c`` here makes the
    stub behave like ``python`` so those subprocesses actually run.

    Returns an exit code to propagate (we handled the invocation), or ``None``
    to fall through to the normal menu-bar launch.
    """
    argv = sys.argv
    if len(argv) >= 2 and argv[1] in ("-m", "-c"):
        # The embedded (Briefcase) Python does NOT install CPython's default
        # SIGPIPE→SIG_IGN, so the MinerU daemon and its spawn render workers
        # would die with SIGPIPE (rc 141) the moment a multiprocessing pipe
        # closes. Restore the normal Python behaviour for every interpreter-mode
        # invocation. (The menu-bar path does this separately for the Cocoa
        # SIGPIPE-reset issue.)
        import signal as _sig

        try:
            _sig.signal(_sig.SIGPIPE, _sig.SIG_IGN)
        except (ValueError, OSError):
            pass
    if len(argv) >= 3 and argv[1] == "-m":
        _ensure_userbase_on_path()  # so the daemon can import mineru/torch
        import runpy

        module = argv[2]
        sys.argv = [module, *argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0
    if len(argv) >= 2 and argv[1] == "-c":
        _ensure_userbase_on_path()  # spawn render workers import mineru too
        code = argv[2] if len(argv) >= 3 else ""
        sys.argv = ["-c", *argv[3:]]
        exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
        return 0
    return None


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
    # Frozen-app interpreter shim — MUST be first so MinerU's parser daemon
    # (-m) and its spawn-based render pool (-c) run Python code instead of
    # relaunching the menu-bar app. See _maybe_run_as_interpreter.
    rc = _maybe_run_as_interpreter()
    if rc is not None:
        return rc
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

    # Lazy pip installs land in userbase/ (prefix layout). Put its
    # site-packages on sys.path + PYTHONPATH so freshly-installed deps import
    # here AND in subprocesses (e.g. the MinerU parser daemon).
    _ensure_userbase_on_path()
    user_prefix = user / "userbase"

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
