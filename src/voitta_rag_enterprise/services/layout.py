"""Per-page layout summaries + char-range → page lookups.

The PDF parser hands us:

* ``page_layout`` — one dict per block, each carrying at least
  ``page`` and ``type``; PDFs from MinerU also include ``bbox`` (PDF
  points, top-left origin), ``text`` for text/title blocks, and
  ``text_level`` for headings.
* ``char_to_page`` — sparse ``(char_offset, page)`` anchors that say
  "from this offset onward, content is on this page until the next
  anchor."

This module turns those into:

* ``compute_page_layout_summary(blocks, page_w, page_h)`` — a flat
  dict of layout scalars (counts, area ratios, ``layout_kind``) that
  Qdrant can index per-payload-field for fast filtering at query time.
* ``primary_page_for_range`` / ``pages_for_range`` — given a chunk's
  ``[char_start, char_end)``, which page anchors it and which set of
  pages it touches.

All functions are pure and side-effect-free so they're trivial to
unit-test against fixture content lists.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Page summary
# ---------------------------------------------------------------------------


_TEXT_TYPES = ("text",)
_TITLE_TYPES = ("title",)
_IMAGE_TYPES = ("image",)
_TABLE_TYPES = ("table",)
_EQUATION_TYPES = ("equation",)
# MinerU also emits a few low-signal block types we count separately:
_HEADER_TYPES = ("header",)
_PAGE_NUMBER_TYPES = ("page_number", "page_footnote")


def compute_page_layout_summary(
    blocks: list[dict],
    page_w: float | None = None,
    page_h: float | None = None,
) -> dict:
    """Return a flat dict of layout scalars for the given page's blocks.

    ``page_w`` / ``page_h`` are optional; when not supplied (or zero)
    we infer them from the maximum bbox bottom-right coords across the
    page. Area-ratio fields fall back to 0.0 when the page area can't
    be inferred (no bboxes at all).

    The returned dict is intentionally flat — every key starts with
    ``layout_`` so it slots straight into a Qdrant payload alongside
    chunk fields without nesting. Booleans are real ``bool`` (Qdrant's
    payload index for booleans needs that type). Fields default to
    safe values (0 / False / "other") so callers can blindly attach
    the dict to every chunk regardless of how rich the page is.
    """
    n_text = 0
    n_title = 0
    n_image = 0
    n_table = 0
    n_equation = 0
    n_header = 0
    n_page_number = 0

    text_area = 0.0
    image_area = 0.0
    inferred_w = page_w or 0.0
    inferred_h = page_h or 0.0

    text_x_starts: list[float] = []
    max_text_level = 0

    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")
        bbox = blk.get("bbox")
        area, x0 = _bbox_area_and_x0(bbox)
        if bbox and isinstance(bbox, list | tuple) and len(bbox) >= 4:
            inferred_w = max(inferred_w, float(bbox[2]))
            inferred_h = max(inferred_h, float(bbox[3]))

        if btype in _TEXT_TYPES:
            n_text += 1
            text_area += area
            if x0 is not None:
                text_x_starts.append(x0)
        elif btype in _TITLE_TYPES:
            n_title += 1
            text_area += area
            if x0 is not None:
                text_x_starts.append(x0)
            level = blk.get("text_level")
            if isinstance(level, int):
                max_text_level = max(max_text_level, level)
        elif btype in _IMAGE_TYPES:
            n_image += 1
            image_area += area
        elif btype in _TABLE_TYPES:
            n_table += 1
            image_area += area
        elif btype in _EQUATION_TYPES:
            n_equation += 1
        elif btype in _HEADER_TYPES:
            n_header += 1
        elif btype in _PAGE_NUMBER_TYPES:
            n_page_number += 1

    page_area = inferred_w * inferred_h if inferred_w and inferred_h else 0.0
    text_density = (text_area / page_area) if page_area > 0 else 0.0
    image_area_ratio = (image_area / page_area) if page_area > 0 else 0.0
    column_count = _column_count(text_x_starts, inferred_w)
    kind = _layout_kind(
        n_text=n_text,
        n_title=n_title,
        n_image=n_image,
        n_table=n_table,
        n_equation=n_equation,
        text_density=text_density,
        image_area_ratio=image_area_ratio,
        column_count=column_count,
        max_text_level=max_text_level,
    )

    return {
        "layout_n_text": n_text,
        "layout_n_title": n_title,
        "layout_n_image": n_image,
        "layout_n_table": n_table,
        "layout_n_equation": n_equation,
        "layout_n_header": n_header,
        "layout_n_page_number": n_page_number,
        "layout_has_image": n_image > 0,
        "layout_has_table": n_table > 0,
        "layout_has_equation": n_equation > 0,
        "layout_text_density": round(text_density, 4),
        "layout_image_area_ratio": round(image_area_ratio, 4),
        "layout_max_text_level": max_text_level,
        "layout_column_count": column_count,
        "layout_kind": kind,
    }


def summaries_by_page(page_layout: list[dict]) -> dict[int, dict]:
    """Group ``page_layout`` by page, summarise each, return ``{page: summary}``.

    Convenience for the indexer — call once per file, then look up by
    ``primary_page`` for each chunk/image. Pages with no blocks are
    absent from the result; consumers should treat missing entries as
    "no layout info" and either skip the layout fields or attach a
    default summary.
    """
    grouped: dict[int, list[dict]] = {}
    for blk in page_layout:
        if not isinstance(blk, dict):
            continue
        page = blk.get("page")
        if not isinstance(page, int):
            continue
        grouped.setdefault(page, []).append(blk)
    return {p: compute_page_layout_summary(blocks) for p, blocks in grouped.items()}


# ---------------------------------------------------------------------------
# char_to_page lookups
# ---------------------------------------------------------------------------


def primary_page_for_range(
    char_to_page: list[tuple[int, int]],
    char_start: int,
    char_end: int,
) -> int | None:
    """Page that anchors a char range. ``None`` when the map is empty.

    "Anchor" = the page covering ``char_start``. We use the page the
    chunk *starts* on rather than the page it occupies the most chars
    on because chunks rarely cross more than one boundary, and start-
    anchored is what humans expect for citations ("see page 12" for a
    chunk that begins on 12 and bleeds into 13).
    """
    if not char_to_page:
        return None
    return _page_at(char_to_page, char_start)


def pages_for_range(
    char_to_page: list[tuple[int, int]],
    char_start: int,
    char_end: int,
) -> list[int]:
    """All pages a char range touches, in order, deduplicated."""
    if not char_to_page or char_end <= char_start:
        return []
    pages: list[int] = []
    seen: set[int] = set()
    # Anchor at start, then walk every (offset, page) inside the range.
    start_page = _page_at(char_to_page, char_start)
    if start_page is not None:
        pages.append(start_page)
        seen.add(start_page)
    # Find the first anchor strictly past char_start; iterate forward.
    offsets = [o for o, _ in char_to_page]
    idx = bisect.bisect_right(offsets, char_start)
    while idx < len(char_to_page) and char_to_page[idx][0] < char_end:
        page = char_to_page[idx][1]
        if page not in seen:
            pages.append(page)
            seen.add(page)
        idx += 1
    return pages


def _page_at(char_to_page: list[tuple[int, int]], offset: int) -> int | None:
    """Page covering ``offset``: largest anchor whose offset ≤ offset."""
    if not char_to_page:
        return None
    offsets = [o for o, _ in char_to_page]
    idx = bisect.bisect_right(offsets, offset) - 1
    if idx < 0:
        # Offset before the first anchor — fall back to the first anchor's
        # page rather than ``None`` so chunks at the very top of a doc
        # still get a sensible page (usually page 1).
        return char_to_page[0][1]
    return char_to_page[idx][1]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _bbox_area_and_x0(bbox) -> tuple[float, float | None]:
    if not isinstance(bbox, list | tuple) or len(bbox) < 4:
        return 0.0, None
    try:
        x0 = float(bbox[0])
        y0 = float(bbox[1])
        x1 = float(bbox[2])
        y1 = float(bbox[3])
    except (TypeError, ValueError):
        return 0.0, None
    w = max(0.0, x1 - x0)
    h = max(0.0, y1 - y0)
    return w * h, x0


def _column_count(x_starts: Iterable[float], page_w: float) -> int:
    """Cluster text-block x-starts into 1 or 2 columns.

    Heuristic: if the fraction of blocks whose x0 lies in the right half
    of the page exceeds 25 %, call it two columns. The threshold trades
    off against documents that occasionally indent — a single right-side
    pull-quote on an otherwise 1-column page should stay 1-column.
    """
    xs = list(x_starts)
    if len(xs) < 4 or page_w <= 0:
        return 1
    midline = page_w / 2.0
    right = sum(1 for x in xs if x > midline)
    return 2 if right / len(xs) > 0.25 else 1


def _layout_kind(
    *,
    n_text: int,
    n_title: int,
    n_image: int,
    n_table: int,
    n_equation: int,
    text_density: float,
    image_area_ratio: float,
    column_count: int,
    max_text_level: int,
) -> str:
    """Coarse-grained page archetype derived from the other scalars.

    Returns one of: ``cover``, ``exhibit``, ``body_2col``, ``body_1col``,
    ``quote``, ``other``. Single label per page so it indexes cleanly as
    a Qdrant keyword. Order of rules is load-bearing — earlier rules
    win, so e.g. "exhibit" trumps "body_*" even on text-rich exhibit
    pages.
    """
    n_blocks = n_text + n_title + n_image + n_table + n_equation
    if n_blocks == 0:
        return "other"
    # Cover: a hero image, a title, and *no* body text. Tighter than the
    # naïve "image + few blocks" rule because cover tends to compete with
    # exhibit (a chart with caption labels) — the no-body-text constraint
    # is what cleanly separates them.
    if (
        n_image >= 1
        and n_text == 0
        and n_title >= 1
        and image_area_ratio > 0.3
    ):
        return "cover"
    # Exhibit: a chart/table dominates the page.
    if image_area_ratio > 0.4 or (n_table >= 1 and n_text <= 4):
        return "exhibit"
    # Pull-quote: tiny block count, no images, large title.
    if n_text <= 1 and n_image == 0 and n_table == 0 and max_text_level >= 1:
        return "quote"
    # Body: the rest, split by column count.
    if n_text + n_title >= 2:
        return "body_2col" if column_count >= 2 else "body_1col"
    return "other"
