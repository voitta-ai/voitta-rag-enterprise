"""Long-lived MinerU worker subprocess.

The parent (``pdf_parser._MineruDaemon``) talks to this process over stdio:
one JSON request per line on stdin, one JSON response per line on stdout.
On any wall-clock timeout the parent SIGKILLs us and respawns; we don't
need to handle that ourselves.

Request: ``{"pdf_path", "page_range", "out_dir", "tier", "parse_mode"}``.
``page_range`` is MinerU's one-based inclusive syntax (``"21-40"``; empty =
whole document). On success the response is ``{"status": "ok"}`` and
``out_dir`` holds ``markdown.md``, ``content_list.json`` and ``images/``
(read back by ``mineru_result.load_saved_result``). On failure it is
``{"status": "error", "code", "detail", "traceback"}``; ``code`` uses
MinerU's error vocabulary (``file_encrypted``, ``file_corrupted``,
``parse_failed``, …) or is ``null`` for anything unclassified.

Why a subprocess at all: MinerU has been observed to wedge in native code
on certain PDFs (no GPU work, no logs, no return). CPython can't deliver
signals into a blocked C thread, so the only durable rescue is process-
level isolation — exactly what this module provides.

Why a daemon (rather than a fresh subprocess per PDF): MinerU loads its
layout / OCR / formula / table models on the first parse (a few seconds)
and keeps them in a process-level singleton. A 358-PDF folder would pay
that 358 times; one warm process pays it once.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

# The daemon writes this file itself (MinerU's ``save()`` does not); the
# parent reads it back via ``mineru_result``. Keep the names in one place.
from .mineru_result import CONTENT_LIST_FILE

# One parser per (tier, parse_mode), kept for the daemon's lifetime. Model
# weights live in MinerU's process singleton either way; reusing the parser
# just avoids re-validating its config per request.
_PARSERS: dict[tuple[str, str], object] = {}

# PDFium load error codes (``pypdfium2.raw.FPDF_ERR_*``) mapped onto MinerU's
# error vocabulary. MinerU 4 surfaces document-load failures as a raw
# ``pypdfium2.PdfiumError`` carrying ``err_code``, not as a ``MineruError``.
_PDFIUM_ERR_CODES = {
    2: "file_corrupted",  # FPDF_ERR_FILE: not found or could not be opened
    3: "file_corrupted",  # FPDF_ERR_FORMAT: not a PDF or corrupted
    4: "file_encrypted",  # FPDF_ERR_PASSWORD: password required or incorrect
    5: "file_encrypted",  # FPDF_ERR_SECURITY: unsupported security scheme
}


def _parser(tier: str, parse_mode: str):
    key = (tier, parse_mode)
    parser = _PARSERS.get(key)
    if parser is None:
        from mineru.parser import MinerUParser

        # image_analysis drives VLM figure descriptions, which only the
        # standard/advanced tiers run; basic accepts and ignores it. Pass
        # False so a tier switch never silently starts calling a VLM.
        parser = MinerUParser(tier=tier, parse_mode=parse_mode, image_analysis=False)
        _PARSERS[key] = parser
    return parser


def _handle_request(req: dict) -> dict:
    # Lazy import — keeps daemon startup near-instant for the common case
    # where the parent spawns us speculatively but never actually sends a
    # request before tearing down (e.g. test runs, healthchecks).
    from mineru.parser import ParseResult
    from mineru.parser.writer import FileBasedDataWriter
    from mineru.render import render_content_list

    out_dir = Path(req["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _parser(req["tier"], req["parse_mode"]).parse(
        req["pdf_path"], page_range=req.get("page_range") or ""
    )
    result.save(FileBasedDataWriter(str(out_dir)))
    # Render the content list from the SAVED middle JSON: before save() the
    # figure blocks still hold inline image bytes and the renderer would emit
    # data URIs as img_path; after it they reference the written images/.
    saved = ParseResult.from_json((out_dir / "middle_json.json").read_text(encoding="utf-8"))
    (out_dir / CONTENT_LIST_FILE).write_text(
        json.dumps(render_content_list(saved.middle_json), ensure_ascii=False),
        encoding="utf-8",
    )
    return {"status": "ok"}


def _error_code(exc: BaseException) -> str | None:
    try:
        from mineru.errors import MineruError
        from pypdfium2 import PdfiumError
    except ImportError:
        # A broken MinerU install: the request's own ImportError is the
        # useful error and must reach the parent, not crash the daemon here.
        return None

    if isinstance(exc, MineruError):
        return exc.code
    if isinstance(exc, PdfiumError):
        return _PDFIUM_ERR_CODES.get(exc.err_code)
    return None


def _exit_when_orphaned(parent_pid: int) -> None:
    """Kill our process group once the parent app is gone.

    We run in our own session (the parent's ``start_new_session``), so the
    server's signals never reach us; stdin EOF only lands between requests.
    If the parent dies mid-parse (SIGKILL, OOM), stop at once instead of
    finishing a long parse on a GPU nobody is waiting for.
    """
    import signal
    import time

    while os.getppid() == parent_pid:
        time.sleep(1.0)
    os.killpg(0, signal.SIGKILL)


def main() -> None:
    import threading

    threading.Thread(
        target=_exit_when_orphaned, args=(os.getppid(),), daemon=True, name="orphan-watch"
    ).start()
    # Keep the response pipe to ourselves. MinerU's render workers inherit
    # fds 0-2, so if fd 1 were the protocol pipe the parent could not see EOF
    # until every worker had exited, and anything a library prints to stdout
    # would corrupt the stream. Move the protocol onto a private dup (Python
    # fds are non-inheritable) and point fd 1 at stderr, which the parent
    # drains into the log.
    proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    while True:
        line = sys.stdin.readline()
        if not line:
            break  # EOF: parent went away.
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = _handle_request(req)
        except Exception as e:
            resp = {
                "status": "error",
                "code": _error_code(e),
                "detail": repr(e),
                "traceback": traceback.format_exc(),
            }
        proto.write(json.dumps(resp) + "\n")
        proto.flush()
    for parser in _PARSERS.values():
        parser.close()


if __name__ == "__main__":
    # CRITICAL: MinerU renders PDF pages in worker processes started with the
    # "spawn" method. A spawned worker re-imports THIS module as ``__main__``
    # to recover globals — which, without this guard, re-runs ``main()`` and
    # blocks the worker forever on ``sys.stdin.readline()``. The render then
    # never returns and the parent's watchdog kills the whole daemon — so
    # every PDF "times out", even tiny ones. ``freeze_support`` + the
    # MainProcess check ensure only the genuine daemon process runs the stdio
    # loop; spawned workers fall through to multiprocessing's own bootstrap.
    multiprocessing.freeze_support()
    if multiprocessing.current_process().name == "MainProcess":
        main()
