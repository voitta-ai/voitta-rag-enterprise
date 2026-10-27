"""Read one saved MinerU parse and translate it into our parser shapes.

The MinerU daemon (``_mineru_subprocess``) writes, per request, into its
``out_dir``:

* ``markdown.md`` — the document body, figures referenced as
  ``![](images/<name>)``;
* ``content_list.json`` — MinerU's flat per-block list (Content List V1),
  rendered from the saved middle JSON so every ``img_path`` is a relative
  ``images/...`` path rather than an inline data URI;
* ``images/`` — the figure crops, named
  ``page_{page_idx}_{block_type}_{n}.{ext}``.

``page_idx`` in the content list is 0-based and refers to the ORIGINAL
document even when the parse was restricted with ``page_range``, so every
page number here is simply ``page_idx + 1``. ``bbox`` values are integers in
per-mille of the page (0-1000 on both axes, top-left origin).

Titles are ``type: "text"`` blocks with an int ``text_level``; MinerU has no
separate ``title`` type.

Everything in this module is a pure function over those files so it can be
unit-tested against recorded fixtures without MinerU installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MARKDOWN_FILE = "markdown.md"
CONTENT_LIST_FILE = "content_list.json"

# Block types whose text MinerU writes into the markdown body, and so can
# anchor a char offset to a page. Headers, footers, page numbers and page
# footnotes are dropped from the markdown, so they must not be searched for:
# a hit would land on unrelated later text and skip the real anchors.
_ANCHOR_TEXT_TYPES = ("text", "list", "code")

# Minimum probe length for a char→page anchor; shorter strings ("Q1", "1.")
# match too often to place reliably.
_MIN_ANCHOR_CHARS = 8
# The first 80 chars of a block are unique enough within one bucket and keep
# a 4 KB paragraph from scanning pathologically.
_ANCHOR_PROBE_CHARS = 80
# Markdown decoration MinerU puts before a block's text on its line: heading
# hashes, list bullets, blockquote markers. An anchor moves back over these so
# a chunk starting at "## Heading" belongs to the heading's page.
_LINE_DECORATION = " \t#>*-+"


@dataclass
class SavedResult:
    markdown: str
    content_list: list[dict]
    root: Path  # the daemon's out_dir; image refs resolve against it


def load_saved_result(out_dir: Path) -> SavedResult:
    """Load what the daemon wrote for one request. Raises if it is incomplete.

    The daemon writes both files on every successful parse, so a missing
    file means the parse did not finish and must not be indexed as empty.
    """
    md_path = out_dir / MARKDOWN_FILE
    cl_path = out_dir / CONTENT_LIST_FILE
    if not md_path.is_file():
        raise RuntimeError(f"MinerU produced no {MARKDOWN_FILE} under {out_dir}")
    if not cl_path.is_file():
        raise RuntimeError(f"MinerU produced no {CONTENT_LIST_FILE} under {out_dir}")
    content_list = json.loads(cl_path.read_text(encoding="utf-8"))
    if not isinstance(content_list, list):
        raise RuntimeError(f"{cl_path} is not a JSON list")
    return SavedResult(
        markdown=md_path.read_text(encoding="utf-8"),
        content_list=[b for b in content_list if isinstance(b, dict)],
        root=out_dir,
    )


def resolve_image(root: Path, ref: str) -> Path | None:
    """Map a markdown image ref to its file under ``root``.

    Returns ``None`` for refs that are missing or point outside ``root``
    (an absolute path or ``..`` in a ref is never followed).
    """
    root = root.resolve()
    candidate = (root / ref).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _page(blk: dict) -> int | None:
    page_idx = blk.get("page_idx")
    # bool is an int subclass; a stray ``true`` must not become page 2.
    if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx < 0:
        return None
    return page_idx + 1


def image_page_map(content_list: list[dict]) -> dict[str, int]:
    """Map each block's ``img_path`` (as written in the markdown) to its page."""
    out: dict[str, int] = {}
    for blk in content_list:
        path = blk.get("img_path")
        page = _page(blk)
        if isinstance(path, str) and path and page is not None:
            out[path] = page
    return out


def layout_blocks(content_list: list[dict]) -> list[dict]:
    """Content-list blocks with an absolute 1-indexed ``page`` key added.

    Every other field MinerU emits (``type``, ``bbox``, ``text``,
    ``text_level``, ``img_path``, captions, ``table_body``, ``list_items``, …)
    passes through unchanged. Blocks without a usable ``page_idx`` are
    dropped — they would land on an unknown page.
    """
    out: list[dict] = []
    for blk in content_list:
        page = _page(blk)
        if page is None:
            continue
        out.append({**blk, "page": page})
    return out


def _anchor_text(blk: dict) -> str | None:
    btype = blk.get("type")
    if btype not in _ANCHOR_TEXT_TYPES:
        return None
    if btype == "list":
        items = blk.get("list_items")
        text = next((i for i in items if isinstance(i, str) and i.strip()), None) if isinstance(items, list) else None
    elif btype == "code":
        text = blk.get("code_body")
    else:
        text = blk.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text if len(text) >= _MIN_ANCHOR_CHARS else None


def char_to_page_anchors(
    content_list: list[dict],
    markdown: str,
    first_page: int,
    offset: int,
) -> list[tuple[int, int]]:
    """``(char_offset, page)`` anchors for one bucket's markdown.

    MinerU writes blocks in reading order, so we walk the content list in
    order and search forward through ``markdown`` from a moving cursor.
    Blocks whose text does not match verbatim (inline formula re-rendering)
    are skipped — their neighbours bracket the page. A match preceded on its
    line only by markdown decoration (``## ``, ``- ``) anchors at the line
    start, so the decoration is on the block's page too.
    ``offset`` is where this bucket's markdown starts in the merged content;
    ``first_page`` anchors the very start of the bucket.
    """
    anchors: list[tuple[int, int]] = [(offset, first_page)]
    search_from = 0
    for blk in content_list:
        page = _page(blk)
        text = _anchor_text(blk)
        if page is None or text is None:
            continue
        probe = text[:_ANCHOR_PROBE_CHARS]
        idx = markdown.find(probe, search_from)
        if idx == -1:
            continue
        line_start = max(markdown.rfind("\n", 0, idx) + 1, search_from)
        decorated = not markdown[line_start:idx].strip(_LINE_DECORATION)
        anchors.append((offset + (line_start if decorated else idx), page))
        search_from = idx + len(probe)
    return anchors
