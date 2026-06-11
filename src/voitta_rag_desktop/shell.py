"""macOS menu-bar shell (rumps) for the Voitta RAG app.

Two phases, both driven from the status-bar item (no Dock icon — LSUIElement):

  1. **Install** — on first launch (or after a version bump) run the installer
     on a background thread, streaming progress into the menu. The server is
     NOT imported until this completes (it needs the heavy stack the installer
     fetches).
  2. **Serve** — start uvicorn(``voitta_rag_enterprise.main:app``) on a worker
     thread with the env ``__main__`` exported (single-user, managed Qdrant),
     wait for ``/healthz``, then open the browser to the SPA.

Thread-safety: AppKit/rumps UI may only be touched on the main thread. Worker
threads therefore never call ``self.menu``/``self.title`` directly — they write
plain Python state (a status string, pending browser/notification flags) and a
main-thread ``rumps.Timer`` reconciles the UI from that state. Mutating the
menu from a worker thread is what hard-crashes a PyObjC app with no traceback.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import rumps

from ._version import __version__

_HOST = "127.0.0.1"
_PORT = 8756  # uncommon port to avoid clashing with a dev server on 8000
_WEBSITE = "https://voitta.ai"
_MCP_PATH = "/mcp"  # FastMCP streamable-http endpoint on the same port
_MCP_SERVER_NAME = "voitta-rag"

log = logging.getLogger("voitta.desktop")


def _mcp_url() -> str:
    return f"http://{_HOST}:{_PORT}{_MCP_PATH}"


def _mcp_config_json() -> str:
    """The MCP client config block for this local server, ready to paste into
    Claude (``.mcp.json`` / claude_desktop_config.json). Single-user mode is on
    for the desktop app, so the server bypasses bearer auth — no token needed
    from localhost, just the streamable-http URL."""
    import json

    return json.dumps(
        {"mcpServers": {_MCP_SERVER_NAME: {"type": "http", "url": _mcp_url()}}},
        indent=2,
    )


def _copy_to_clipboard(text: str) -> bool:
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString

        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.setString_forType_(text, NSPasteboardTypeString))
    except Exception:  # noqa: BLE001
        log.exception("clipboard copy failed")
        return False


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _dir_size(path: Path) -> int:
    """Total bytes under ``path`` — via ``du`` (fast C scan) so the About dialog
    opens instantly even over a multi-GB data dir. Returns 0 on any failure."""
    import subprocess

    try:
        out = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return int(out.stdout.split()[0]) * 1024
    except Exception:  # noqa: BLE001
        return 0


def _setup_logging() -> None:
    """Timestamped lines to stdout (which ``__main__`` has pointed at the
    logfile). Called from ``main`` — after the stdout redirect is in place — so
    the handler captures the file, not the now-dead terminal."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")
    )
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False  # don't double-log through uvicorn's root config
    # NOTE: do NOT wire handlers for the enterprise app's loggers here — the
    # server's logging_config.setup_logging() dictConfig replaces them at app
    # startup anyway. The app's own logs land under <data_dir>/logs/ (see
    # docs/OPERATIONS.md §12); voitta-rag.log carries only this shell,
    # uvicorn, and raw stdout/stderr.


class _LineTee:
    """Write-through stream that forwards each completed line to a sink.

    During first-run install we point sys.stdout/stderr at this so pip's and the
    downloader's output lands in BOTH the logfile (the underlying stream) and
    the install window's log view (the sink), with no extra plumbing in the
    installer.
    """

    def __init__(self, underlying, sink) -> None:
        self._u = underlying
        self._sink = sink
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self._u.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            try:
                self._sink(line)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        try:
            self._u.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        # Some libraries probe the real fd; delegate so they don't crash. Output
        # written via the fd directly bypasses the tee (logfile only) — fine.
        return self._u.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._u, "encoding", "utf-8")


def _window_reporter(win):
    """An installer.InstallReporter that also drives the InstallWindow rows.

    Text (reporter.log + the phase markers printed by the base class) reaches
    the window via the stdout tee, so this only needs to mirror phase
    transitions onto the visual rows. Defined as a function so install_window /
    installer imports stay lazy (heavy AppKit import only when setup is needed).
    """
    from . import installer

    class _WindowReporter(installer.InstallReporter):
        def phase_start(self, phase, label="Running…"):
            super().phase_start(phase, label)
            win.start_phase(phase, label)

        def phase_progress(self, phase, current, total, label):
            win.update_phase(phase, current, total, label)  # row only — no log spam

        def phase_done(self, phase, note="Done"):
            super().phase_done(phase, note)
            win.finish_phase(phase, note)

        def phase_skip(self, phase, note="Already installed"):
            super().phase_skip(phase, note)
            win.skip_phase(phase, note)

        def phase_fail(self, phase, reason):
            super().phase_fail(phase, reason)
            win.fail_phase(phase, reason)

    return _WindowReporter()


class VoittaRagApp(rumps.App):
    def __init__(self, user_data_dir: Path, resources_dir: Path) -> None:
        # The "RAG" wordmark is baked into the icon image (see
        # desktop/make_menubar_icon.py). rumps' legacy NSStatusItem API won't
        # reliably show a title and an image together — passing both drops the
        # image — so we use icon-only with NO title. template=True: it's a flat
        # monochrome wordmark, so macOS tints it to match the menu bar (black on
        # light, white on dark), looking native beside the other status icons.
        icon = resources_dir / "voitta-menubar.png"
        super().__init__(
            "Voitta RAG",
            icon=str(icon) if icon.is_file() else None,
            template=True,
            quit_button=None,
        )
        self._user_data_dir = user_data_dir
        self._resources_dir = resources_dir
        self._server = None  # uvicorn.Server, set once started
        self._install_win = None  # InstallWindow during first-run setup, else None
        self._about = None  # AboutController while the About window is open

        # Worker-thread → main-thread channel (plain Python, no AppKit).
        self._lock = threading.Lock()
        self._status = "Starting…"
        self._ready = False
        self._pending_browser = False
        self._pending_notice: tuple[str, str] | None = None
        self._last_rendered = ""
        self._ui_ticks = 0  # main-thread tick count; gates the SIGPIPE re-assert

        self._status_item = rumps.MenuItem("Starting…")
        self._status_item.set_callback(None)  # non-clickable status line
        self.menu = [
            self._status_item,
            None,
            rumps.MenuItem("Open Voitta RAG", callback=self._on_open),
            rumps.MenuItem("About Voitta RAG", callback=self._on_about),
            rumps.MenuItem("Show Logs", callback=self._on_logs),
            None,
            rumps.MenuItem("Quit", callback=self._on_quit),
        ]

        # Main-thread UI reconciler — the ONLY place that mutates rumps state.
        self._ui_timer = rumps.Timer(self._sync_ui, 0.4)
        self._ui_timer.start()

        # Heavy work off the main thread.
        threading.Thread(target=self._bootstrap, daemon=True).start()

    # -- thread-safe state setters (called from worker threads) --------------

    def _set_status(self, text: str) -> None:
        with self._lock:
            self._status = text

    def _notify(self, title: str, message: str) -> None:
        with self._lock:
            self._pending_notice = (title, message)

    # -- main-thread UI reconciler -------------------------------------------

    def _sync_ui(self, _timer) -> None:
        # The enterprise app + uvicorn call logging.config.dictConfig during
        # startup with disable_existing_loggers=True, which silences our logger
        # mid-run (it's how earlier lifecycle diagnostics vanished). This timer
        # runs on the main thread every 0.4s — cheaply re-assert so the logger
        # is never disabled for more than a tick.
        if log.disabled:
            log.disabled = False
        with self._lock:
            self._ui_ticks += 1
            status = self._status
            notice = self._pending_notice
            self._pending_notice = None

        # Cocoa/NSApplication can reset SIGPIPE to its default (terminate)
        # disposition during/after launch, which silently kills the process the
        # moment the server thread writes to a half-closed browser socket.
        # signal.signal() is only legal on the main thread — this timer IS the
        # main thread — so re-assert SIG_IGN for the first few ticks to cover
        # the startup window. (Set once at process start in __main__ too.)
        if self._ui_ticks <= 12:
            try:
                import signal

                signal.signal(signal.SIGPIPE, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        if status != self._last_rendered:
            self._status_item.title = status
            self._last_rendered = status
        if notice is not None:
            try:
                rumps.notification("Voitta RAG", notice[0], notice[1])
            except Exception:
                pass

    # -- bootstrap (install then serve) --------------------------------------

    def _make_install_window(self):
        """Create + show the InstallWindow on the main (AppKit) thread.

        _bootstrap runs on a worker thread, but NSWindow construction must be on
        the main thread — so hop via callAfter and block briefly for the handle.
        Returns None if AppKit/window creation fails (install then proceeds
        headless, logging to the file as usual)."""
        from PyObjCTools import AppHelper

        holder: dict[str, object] = {}
        ready = threading.Event()

        def _create() -> None:
            try:
                from .install_window import InstallWindow

                w = InstallWindow()
                w.show()
                holder["win"] = w
            except Exception:  # noqa: BLE001
                log.exception("install window creation failed — going headless")
            finally:
                ready.set()

        AppHelper.callAfter(_create)
        ready.wait(timeout=15)
        return holder.get("win")

    def _bootstrap(self) -> None:
        from . import installer

        complete = installer.is_install_complete(self._resources_dir)
        log.info("bootstrap: starting (install_complete=%s)", complete)
        if not complete:
            self._set_status("Setting up…")
            win = self._make_install_window()
            self._install_win = win
            reporter = _window_reporter(win) if win else installer.InstallReporter()

            # Tee pip/download output into the window log (and keep the logfile).
            saved = (sys.stdout, sys.stderr)
            if win is not None:
                tee = _LineTee(saved[0], win.log)
                sys.stdout = sys.stderr = tee
            try:
                installer.install_all(self._resources_dir, reporter)
            except Exception as exc:  # noqa: BLE001
                log.exception("install failed")
                self._set_status(f"Install failed: {exc}")
                self._notify("Install failed", str(exc))
                if win is not None:
                    win.log(f"!!! setup failed: {exc}")
                return  # leave the window open so the user can read the error
            finally:
                sys.stdout, sys.stderr = saved
            log.info("bootstrap: install complete")

        # CA bundle for the server thread's HTTPS (HF model pulls), including
        # the fast path where install was already complete.
        installer._ensure_ca_env()
        self._start_server()

    # -- server --------------------------------------------------------------

    def _start_server(self) -> None:
        self._set_status("Starting server…")

        def _run() -> None:
            log.info("server: starting uvicorn on %s:%s", _HOST, _PORT)
            try:
                import copy

                import uvicorn
                from uvicorn.config import LOGGING_CONFIG

                from voitta_rag_enterprise.main import app

                # uvicorn's default logging config has disable_existing_loggers
                # =True, which silences our "voitta.desktop" logger the instant
                # the server starts (this is why earlier diagnostics vanished
                # mid-run). Keep uvicorn's formatting but stop it disabling us.
                log_config = copy.deepcopy(LOGGING_CONFIG)
                log_config["disable_existing_loggers"] = False

                config = uvicorn.Config(
                    app, host=_HOST, port=_PORT, log_level="info",
                    log_config=log_config,
                    ws_ping_interval=30, ws_ping_timeout=90,
                )
                self._server = uvicorn.Server(config)
                self._server.run()
                # run() returns normally only when should_exit is set (Quit).
                # If it returns otherwise the server died on its own — surface it
                # rather than leaving a closed port behind a live menu bar.
                if not getattr(self._server, "should_exit", None):
                    log.error("server: uvicorn stopped unexpectedly")
                    self._set_status("Server stopped — see logs")
                    self._notify("Server stopped", "uvicorn exited on its own — see logs")
            except Exception as exc:  # noqa: BLE001
                log.exception("server: uvicorn crashed")
                self._set_status(f"Server crashed: {exc}")
                self._notify("Server crashed", str(exc))

        threading.Thread(target=_run, daemon=True, name="server").start()
        threading.Thread(target=self._await_ready, daemon=True, name="await-ready").start()

    def _await_ready(self) -> None:
        url = f"http://{_HOST}:{_PORT}/healthz"
        deadline = time.monotonic() + 300
        attempt = 0
        log.info("await-ready: polling %s", url)
        while time.monotonic() < deadline:
            attempt += 1
            try:
                with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310
                    status = r.status
                if status == 200:
                    # App startup's dictConfig may have just disabled us (see
                    # _sync_ui) within this sub-tick window — re-assert so this
                    # milestone is never lost to that race.
                    log.disabled = False
                    log.info("await-ready: healthz 200 on attempt %d — going live", attempt)
                    self._on_ready()
                    return
                log.debug("await-ready: attempt %d got status %s", attempt, status)
            except (urllib.error.URLError, OSError) as exc:
                if attempt == 1 or attempt % 10 == 0:
                    log.debug("await-ready: attempt %d not up yet: %s", attempt, exc)
            except Exception:  # noqa: BLE001 — never let this thread die silently
                log.exception("await-ready: unexpected error on attempt %d", attempt)
            time.sleep(1.5)
        log.error("await-ready: server not ready within 300s")
        self._set_status("Server did not become ready — see logs")

    def _on_ready(self) -> None:
        """Go-live side effects, isolated so a failure in one can't silently
        kill the readiness thread before the others run."""
        with self._lock:
            self._ready = True
        self._set_status(f"Running · v{__version__}")
        if self._install_win is not None:
            try:
                self._install_win.close()
            except Exception:  # noqa: BLE001
                log.exception("await-ready: failed to close install window")
            self._install_win = None
        try:
            webbrowser.open(f"http://{_HOST}:{_PORT}/")
        except Exception:  # noqa: BLE001
            log.exception("await-ready: webbrowser.open failed")

    # -- menu actions (these run on the main thread already) -----------------

    def _on_open(self, _) -> None:
        with self._lock:
            ready = self._ready
            status = self._status
        if ready:
            webbrowser.open(f"http://{_HOST}:{_PORT}/")
        else:
            rumps.notification("Voitta RAG", "Not ready yet", status)

    def _on_about(self, _) -> None:
        """Open the About window — a real titled/closable NSWindow (red close
        button, ESC to close) with an aligned info block and a monospace MCP
        config box. Runs on the main thread (menu callbacks do)."""
        with self._lock:
            status = self._status
        root = os.environ.get("VOITTA_ROOT_PATH", "—")
        data_dir = Path(os.environ.get("VOITTA_DATA_DIR", self._user_data_dir))
        used = _human_bytes(_dir_size(self._user_data_dir))
        qmode = os.environ.get("VOITTA_QDRANT_MODE", "—")
        rows = [
            ("Version", __version__),
            ("Status", status),
            ("Website", _WEBSITE),
            ("Local URL", f"http://{_HOST}:{_PORT}/"),
            ("MCP endpoint", _mcp_url()),
            ("Indexed root", str(root)),
            ("Data folder", str(data_dir)),
            ("Disk used", used),
            ("Qdrant mode", qmode),
        ]

        def _on_copy(config: str) -> None:
            if _copy_to_clipboard(config):
                rumps.notification(
                    "Voitta RAG", "MCP config copied",
                    "Paste it into your Claude MCP settings.",
                )

        try:
            from .about_window import AboutController

            # Retain on the app for the window's lifetime (buttons target it).
            self._about = AboutController(
                rows=rows, config_json=_mcp_config_json(), on_copy=_on_copy
            )
            self._about.show()
        except Exception:  # noqa: BLE001
            log.exception("about window failed")

    def _on_logs(self, _) -> None:
        import subprocess

        log_file = self._user_data_dir / "voitta-rag.log"
        subprocess.run(["open", str(log_file)], check=False)

    def _on_quit(self, _) -> None:
        log.info("quit requested from menu")
        if self._server is not None:
            self._server.should_exit = True
        rumps.quit_application()


def main(user_data_dir: Path, resources_dir: Path) -> int:
    _setup_logging()
    log.info("shell main: starting rumps event loop")
    try:
        VoittaRagApp(user_data_dir, resources_dir).run()
    except Exception:
        log.exception("shell main: rumps run() raised")
        raise
    log.info("shell main: rumps run() returned — app exiting")
    return 0
