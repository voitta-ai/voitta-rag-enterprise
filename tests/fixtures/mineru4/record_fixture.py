"""Record the MinerU 4 fixtures used by ``tests/unit/test_mineru_result.py``.

Drives the real parser daemon (``pdf_parser._MineruDaemon``) exactly as the
app does, then copies what it saved into this directory:

    python tests/fixtures/mineru4/record_fixture.py

* ``full/`` — ``sample.pdf`` parsed whole;
* ``range_2_3/`` — the same file restricted to ``page_range="2-3"``.

Each holds ``markdown.md``, ``content_list.json`` and ``images/``. Needs
MinerU 4 and its ``basic``-tier models; run it on a machine with a GPU after
a MinerU upgrade and review the diff before committing.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from voitta_rag_enterprise.services.parsers import mineru_result
from voitta_rag_enterprise.services.parsers.pdf_parser import _MineruDaemon

HERE = Path(__file__).resolve().parent


def _record(daemon: _MineruDaemon, page_range: str, name: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        daemon.parse(
            pdf_path=HERE / "sample.pdf",
            page_range=page_range,
            out_dir=out,
            tier="basic",
            parse_mode="auto",
            timeout_s=600,
        )
        dest = HERE / name
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir()
        for fname in (mineru_result.MARKDOWN_FILE, mineru_result.CONTENT_LIST_FILE):
            shutil.copy2(out / fname, dest / fname)
        if (out / "images").is_dir():
            shutil.copytree(out / "images", dest / "images")
    print("recorded", dest)


def main() -> None:
    daemon = _MineruDaemon()
    try:
        _record(daemon, "", "full")
        _record(daemon, "2-3", "range_2_3")
    finally:
        # EOF on stdin is the daemon's clean-shutdown path (closes parsers).
        if daemon._proc is not None:
            daemon._proc.stdin.close()
            print("daemon exit code", daemon._proc.wait(timeout=60))


if __name__ == "__main__":
    main()
