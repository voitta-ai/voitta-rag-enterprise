"""Long-lived MinerU worker subprocess.

The parent (``pdf_parser._MineruDaemon``) talks to this process over stdio:
one JSON request per line on stdin, one JSON response per line on stdout.
On any wall-clock timeout the parent SIGKILLs us and respawns; we don't
need to handle that ourselves.

Why a subprocess at all: MinerU has been observed to wedge in native code
on certain PDFs (no GPU work, no logs, no return). CPython can't deliver
signals into a blocked C thread, so the only durable rescue is process-
level isolation — exactly what this module provides.

Why a daemon (rather than a fresh subprocess per PDF): MinerU's first
``do_parse`` call loads several models from disk (Layout / MFR / Table-OCR
det / Table-OCR rec / Table-wireless). On the user's box that's roughly 5
seconds; a 358-PDF folder would pay it 358 times. Loading once and
keeping the process alive amortises that cost across the whole queue.
"""

from __future__ import annotations

import json
import sys
import traceback


def _handle_request(req: dict) -> dict:
    # Lazy import — keeps daemon startup near-instant for the common case
    # where the parent spawns us speculatively but never actually sends a
    # request before tearing down (e.g. test runs, healthchecks).
    from mineru.cli.common import do_parse, read_fn

    pdf_bytes = read_fn(req["bucket_path"])
    do_parse(
        output_dir=req["out_root"],
        pdf_file_names=[req["pdf_name"]],
        pdf_bytes_list=[pdf_bytes],
        p_lang_list=[req["lang"]],
        backend="pipeline",
        parse_method=req["method"],
        formula_enable=True,
        table_enable=True,
        f_draw_layout_bbox=False,
        f_draw_span_bbox=False,
        f_dump_md=True,
        f_dump_middle_json=False,
        f_dump_model_output=False,
        f_dump_orig_pdf=False,
        f_dump_content_list=False,
    )
    return {"status": "ok"}


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return  # EOF: parent went away.
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = _handle_request(req)
        except Exception as e:
            resp = {
                "status": "error",
                "detail": repr(e),
                "traceback": traceback.format_exc(),
            }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
