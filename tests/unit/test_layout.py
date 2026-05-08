"""Unit tests for ``services.layout`` — pure functions, no I/O."""

from __future__ import annotations

from voitta_rag_enterprise.services.layout import (
    _layout_kind,
    compute_page_layout_summary,
    pages_for_range,
    primary_page_for_range,
    summaries_by_page,
)


# ---------------------------------------------------------------------------
# compute_page_layout_summary
# ---------------------------------------------------------------------------


def _txt(text: str, bbox: list[float], level: int | None = None) -> dict:
    """A MinerU-shaped text block. ``level`` non-None marks it as a title."""
    blk: dict = {
        "type": "title" if level is not None else "text",
        "text": text,
        "bbox": bbox,
        "page_idx": 0,
    }
    if level is not None:
        blk["text_level"] = level
    return blk


def _img(bbox: list[float]) -> dict:
    return {"type": "image", "img_path": "x.jpg", "bbox": bbox, "page_idx": 0}


def _table(bbox: list[float]) -> dict:
    return {"type": "table", "img_path": "t.jpg", "bbox": bbox, "page_idx": 0}


def _eq() -> dict:
    return {"type": "equation", "page_idx": 0}


def test_summary_counts_block_types() -> None:
    blocks = [
        _txt("a", [50, 50, 500, 100]),
        _txt("b", [50, 110, 500, 200]),
        _txt("Title", [50, 0, 500, 40], level=1),
        _img([50, 220, 500, 400]),
        _table([50, 410, 500, 600]),
        _eq(),
    ]
    s = compute_page_layout_summary(blocks)
    assert s["layout_n_text"] == 2
    assert s["layout_n_title"] == 1
    assert s["layout_n_image"] == 1
    assert s["layout_n_table"] == 1
    assert s["layout_n_equation"] == 1
    assert s["layout_has_image"] is True
    assert s["layout_has_table"] is True
    assert s["layout_has_equation"] is True
    assert s["layout_max_text_level"] == 1


def test_summary_kind_cover_page() -> None:
    """Cover: a hero image dominates, only a title or two of text."""
    blocks = [
        _txt("Quarterly Report", [50, 700, 550, 760], level=1),
        _img([50, 50, 550, 650]),  # ~83 % of a 600x800 page area
    ]
    s = compute_page_layout_summary(blocks, page_w=600, page_h=800)
    # Cover wins because image_area_ratio > 0.2 and text+title <= 3.
    assert s["layout_kind"] == "cover"


def test_summary_kind_exhibit_page() -> None:
    """Exhibit: chart/table dominates."""
    blocks = [
        _txt("Caption text", [40, 720, 560, 770]),
        _img([40, 40, 560, 700]),  # ~85 % of page
    ]
    s = compute_page_layout_summary(blocks, page_w=600, page_h=800)
    assert s["layout_kind"] == "exhibit"


def test_summary_kind_body_two_column() -> None:
    blocks = [
        _txt("left col 1", [50, 100, 290, 150]),
        _txt("left col 2", [50, 160, 290, 220]),
        _txt("right col 1", [320, 100, 560, 150]),
        _txt("right col 2", [320, 160, 560, 220]),
    ]
    s = compute_page_layout_summary(blocks, page_w=600, page_h=800)
    assert s["layout_column_count"] == 2
    assert s["layout_kind"] == "body_2col"


def test_summary_kind_body_one_column() -> None:
    blocks = [
        _txt("a", [50, 100, 550, 150]),
        _txt("b", [50, 160, 550, 220]),
        _txt("c", [50, 230, 550, 290]),
        _txt("d", [50, 300, 550, 360]),
    ]
    s = compute_page_layout_summary(blocks, page_w=600, page_h=800)
    assert s["layout_column_count"] == 1
    assert s["layout_kind"] == "body_1col"


def test_summary_handles_missing_bbox() -> None:
    blocks = [{"type": "text", "text": "no bbox", "page_idx": 0}]
    s = compute_page_layout_summary(blocks)
    assert s["layout_n_text"] == 1
    # No bbox → can't infer page area → density / ratio default to 0.
    assert s["layout_text_density"] == 0.0
    assert s["layout_image_area_ratio"] == 0.0


def test_summary_empty_blocks_returns_other_kind() -> None:
    s = compute_page_layout_summary([])
    assert s["layout_kind"] == "other"
    assert s["layout_n_text"] == 0


def test_layout_kind_function_directly() -> None:
    """Spot-check the rule order without the bbox plumbing."""
    assert (
        _layout_kind(
            n_text=0,
            n_title=1,
            n_image=1,
            n_table=0,
            n_equation=0,
            text_density=0.05,
            image_area_ratio=0.6,
            column_count=1,
            max_text_level=1,
        )
        == "cover"
    )
    # exhibit beats body when image_area_ratio is dominant
    assert (
        _layout_kind(
            n_text=8,
            n_title=0,
            n_image=0,
            n_table=1,
            n_equation=0,
            text_density=0.4,
            image_area_ratio=0.5,
            column_count=2,
            max_text_level=0,
        )
        == "exhibit"
    )


# ---------------------------------------------------------------------------
# summaries_by_page
# ---------------------------------------------------------------------------


def test_summaries_by_page_groups_correctly() -> None:
    layout = [
        {"type": "text", "page": 1, "bbox": [10, 10, 100, 50], "text": "a"},
        {"type": "image", "page": 1, "bbox": [10, 60, 200, 300]},
        {"type": "text", "page": 2, "bbox": [10, 10, 100, 50], "text": "b"},
        {"type": "noise"},  # missing page → dropped
    ]
    by_page = summaries_by_page(layout)
    assert set(by_page.keys()) == {1, 2}
    assert by_page[1]["layout_n_image"] == 1
    assert by_page[2]["layout_n_image"] == 0


# ---------------------------------------------------------------------------
# char_to_page lookups
# ---------------------------------------------------------------------------


def test_primary_page_picks_anchoring_page() -> None:
    cmap = [(0, 1), (200, 2), (450, 3)]
    assert primary_page_for_range(cmap, 0, 100) == 1
    assert primary_page_for_range(cmap, 250, 400) == 2
    assert primary_page_for_range(cmap, 500, 600) == 3
    # Range that starts before any anchor falls back to the first page.
    assert primary_page_for_range([(50, 7)], 0, 10) == 7


def test_pages_for_range_covers_crossings() -> None:
    cmap = [(0, 1), (200, 2), (450, 3)]
    # Spans a single anchor.
    assert pages_for_range(cmap, 100, 250) == [1, 2]
    # Spans two anchors.
    assert pages_for_range(cmap, 100, 500) == [1, 2, 3]
    # Wholly inside one page.
    assert pages_for_range(cmap, 220, 400) == [2]


def test_lookups_empty_or_zero_range() -> None:
    assert primary_page_for_range([], 0, 100) is None
    assert pages_for_range([], 0, 100) == []
    # Zero-length range returns no pages.
    assert pages_for_range([(0, 1)], 50, 50) == []
