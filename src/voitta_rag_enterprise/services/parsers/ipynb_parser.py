"""Jupyter notebook (.ipynb) → embedding-friendly markdown.

A notebook is JSON wrapping ``cells[]``: ``cell_type`` is ``markdown`` |
``code`` | ``raw``, and ``source`` is the cell's text (string *or* list
of strings — the spec allows either). Code cells additionally carry an
``outputs[]`` array which is where the noise lives: 200KB+ base64
``image/png`` blobs, kernel metadata, execution counts. None of that
helps embeddings; some of it actively hurts (a single matplotlib figure
can blow past the embedder's token cap).

This parser keeps the signal — markdown bodies + code source — and
strips the noise. Optionally it folds short text streams (``stdout``,
``stderr``, ``execute_result`` text) back in, capped per cell, because
they sometimes carry the "what went wrong / what was the answer" signal
search would care about. Anything ``image/*`` is dropped unconditionally.

Output shape is plain markdown:

* markdown cells round-trip as-is;
* code cells become fenced ```<lang> blocks (language read from
  ``metadata.kernelspec.language``, default ``python``);
* text outputs become a fenced ```text block immediately after the code.

Downstream the standard ``ParagraphStrategy`` chunker takes over — the
cell boundaries we emit (one blank line between cells) line up with its
paragraph-boundary preference, so chunks tend to land on cell breaks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from ..indexing_caps import get_caps
from .base import BaseParser, ParserResult


def _coerce_source(source: Any) -> str:
    """``cells[].source`` is allowed to be a string OR a list of strings.

    Both forms are valid notebook JSON; normalize to one string.
    """
    if isinstance(source, list):
        return "".join(s for s in source if isinstance(s, str))
    if isinstance(source, str):
        return source
    return ""


def _text_outputs(outputs: list[dict], cap: int) -> str:
    """Pull non-image text out of a code cell's outputs, capped at ``cap``.

    Keeps:
    * ``stream`` — stdout / stderr writes
    * ``execute_result`` and ``display_data`` text/plain (the cell's "value")
    * ``error`` traceback (helps search land on broken-cell explanations)

    Drops everything image-shaped (``image/png``, ``image/jpeg``,
    ``image/svg+xml``, …) and rich representations we can't usefully
    embed (``application/vnd.*``, ``application/json``).
    """
    pieces: list[str] = []
    for o in outputs:
        if not isinstance(o, dict):
            continue
        otype = o.get("output_type")
        if otype == "stream":
            pieces.append(_coerce_source(o.get("text")))
        elif otype in ("execute_result", "display_data"):
            data = o.get("data") or {}
            if isinstance(data, dict):
                tp = data.get("text/plain")
                if tp is not None:
                    pieces.append(_coerce_source(tp))
        elif otype == "error":
            tb = o.get("traceback") or []
            if isinstance(tb, list):
                pieces.append("\n".join(str(line) for line in tb))
    text = "\n".join(p for p in pieces if p).strip()
    if len(text) > cap:
        text = text[:cap].rstrip() + "\n…[truncated]"
    return text


class IpynbParser(BaseParser):
    """Parser for Jupyter notebooks. Strips outputs, keeps source."""

    extensions: ClassVar[list[str]] = [".ipynb"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as e:
            return ParserResult.failure(f"ipynb read failed: {e}")
        try:
            nb = json.loads(raw)
        except json.JSONDecodeError as e:
            return ParserResult.failure(f"ipynb json decode failed: {e}")
        if not isinstance(nb, dict):
            return ParserResult.failure("ipynb top-level is not an object")

        # Language hint for fenced code blocks. Notebooks for other kernels
        # (R, Julia, Scala, JS) carry the right name here; fall back to
        # ``python`` because that's overwhelmingly the right guess.
        lang = "python"
        ks = (nb.get("metadata") or {}).get("kernelspec") or {}
        if isinstance(ks, dict):
            cand = ks.get("language") or ks.get("name")
            if isinstance(cand, str) and cand.strip():
                lang = cand.strip()

        cells = nb.get("cells")
        if not isinstance(cells, list):
            return ParserResult(content="")

        out: list[str] = []
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            src = _coerce_source(cell.get("source")).rstrip()
            if not src:
                continue
            ctype = cell.get("cell_type")
            if ctype == "markdown":
                out.append(src)
            elif ctype == "code":
                out.append(f"```{lang}\n{src}\n```")
                outputs = cell.get("outputs")
                if isinstance(outputs, list):
                    text = _text_outputs(outputs, get_caps().ipynb_max_output_chars)
                    if text:
                        out.append(f"```text\n{text}\n```")
            elif ctype == "raw":
                out.append(src)

        return ParserResult(content="\n\n".join(out))
