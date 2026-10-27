"""Unit tests for ``services.parsers.mineru_result`` and the bucket merge.

The fixtures under ``tests/fixtures/mineru4/`` are real MinerU 4 ``basic``
output recorded through our daemon (see ``record_fixture.py``): a 3-page
synthetic report with a chart (p1), a table (p2) and a two-column page (p3),
parsed whole (``full/``) and with ``page_range="2-3"`` (``range_2_3/``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from voitta_rag_enterprise.services import layout
from voitta_rag_enterprise.services.parsers import mineru_result, pdf_parser

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mineru4"


@pytest.fixture
def full() -> mineru_result.SavedResult:
    return mineru_result.load_saved_result(FIXTURES / "full")


@pytest.fixture
def ranged() -> mineru_result.SavedResult:
    return mineru_result.load_saved_result(FIXTURES / "range_2_3")


# ---------------------------------------------------------------------------
# load_saved_result
# ---------------------------------------------------------------------------


def test_load_reads_markdown_and_content_list(full: mineru_result.SavedResult) -> None:
    assert full.markdown.startswith("## Annual Operating Review")
    assert {b["type"] for b in full.content_list} == {"text", "chart", "table"}
    assert full.root == FIXTURES / "full"


@pytest.mark.parametrize("missing", ["markdown.md", "content_list.json"])
def test_load_refuses_an_incomplete_parse(tmp_path: Path, missing: str) -> None:
    """A missing file means the parse did not finish — never index it as empty."""
    shutil.copytree(FIXTURES / "full", tmp_path / "out")
    (tmp_path / "out" / missing).unlink()
    with pytest.raises(RuntimeError, match=missing):
        mineru_result.load_saved_result(tmp_path / "out")


def test_load_refuses_a_non_list_content_list(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "full", tmp_path / "out")
    (tmp_path / "out" / "content_list.json").write_text('{"pages": []}')
    with pytest.raises(RuntimeError, match="not a JSON list"):
        mineru_result.load_saved_result(tmp_path / "out")


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def test_every_markdown_image_ref_resolves(full: mineru_result.SavedResult) -> None:
    refs = pdf_parser._IMG_REF_RE.findall(full.markdown)
    assert refs == ["images/page_0_chart_3.jpg"]
    path = mineru_result.resolve_image(full.root, refs[0])
    assert path is not None and path.read_bytes()[:2] == b"\xff\xd8"  # JPEG


@pytest.mark.parametrize(
    "ref", ["images/nope.jpg", "../range_2_3/markdown.md", "/etc/passwd", "images/../../x"]
)
def test_resolve_image_rejects_missing_and_escaping_refs(
    full: mineru_result.SavedResult, ref: str
) -> None:
    assert mineru_result.resolve_image(full.root, ref) is None


def test_image_page_map_uses_absolute_pages(
    full: mineru_result.SavedResult, ranged: mineru_result.SavedResult
) -> None:
    assert mineru_result.image_page_map(full.content_list) == {
        "images/page_0_chart_3.jpg": 1,
        "images/page_1_table_2.jpg": 2,
    }
    # page_range="2-3": MinerU still reports the table on page 2 of the
    # original document, not page 1 of the range.
    assert mineru_result.image_page_map(ranged.content_list) == {
        "images/page_1_table_2.jpg": 2,
    }


# ---------------------------------------------------------------------------
# layout blocks
# ---------------------------------------------------------------------------


def test_layout_blocks_carry_page_and_pass_fields_through(
    full: mineru_result.SavedResult,
) -> None:
    blocks = mineru_result.layout_blocks(full.content_list)
    assert len(blocks) == len(full.content_list)
    assert [b["page"] for b in blocks] == [b["page_idx"] + 1 for b in full.content_list]
    chart = next(b for b in blocks if b["type"] == "chart")
    assert chart["chart_caption"] == ["Figure 1. Revenue by quarter, in millions of dollars."]
    assert all(0 <= v <= 1000 for b in blocks for v in b["bbox"])  # per-mille


def test_layout_blocks_drop_unusable_page_idx() -> None:
    blocks = mineru_result.layout_blocks(
        [
            {"type": "text", "text": "ok", "page_idx": 0},
            {"type": "text", "text": "no page"},
            {"type": "text", "text": "str page", "page_idx": "1"},
            {"type": "text", "text": "bool page", "page_idx": True},
            {"type": "text", "text": "negative", "page_idx": -1},
        ]
    )
    assert [b["text"] for b in blocks] == ["ok"]


def test_fixture_pages_summarise_as_expected(full: mineru_result.SavedResult) -> None:
    """The layout summary sees MinerU 4 headings (text + text_level) as titles,
    the chart as an image and page 3 as two columns."""
    by_page = layout.summaries_by_page(mineru_result.layout_blocks(full.content_list))
    assert by_page[1]["layout_n_title"] == 2
    assert by_page[1]["layout_n_image"] == 1
    assert by_page[2]["layout_n_table"] == 1
    assert by_page[3]["layout_column_count"] == 2
    assert by_page[3]["layout_kind"] == "body_2col"


# ---------------------------------------------------------------------------
# char→page anchors
# ---------------------------------------------------------------------------


def test_anchors_follow_the_pages(full: mineru_result.SavedResult) -> None:
    md = full.markdown
    anchors = mineru_result.char_to_page_anchors(full.content_list, md, 1, 0)
    assert anchors[0] == (0, 1)
    offsets = [o for o, _ in anchors]
    assert offsets == sorted(offsets)
    page_of = dict((p, o) for o, p in reversed(anchors))  # first offset per page
    assert md[page_of[2]:].startswith("## 2. Segment results")
    assert md[page_of[3]:].startswith("## 3. Outlook")


def test_anchors_are_offset_into_the_merged_content(ranged: mineru_result.SavedResult) -> None:
    anchors = mineru_result.char_to_page_anchors(ranged.content_list, ranged.markdown, 2, 5000)
    assert anchors[0] == (5000, 2)
    assert {p for _, p in anchors} == {2, 3}
    assert all(o >= 5000 for o, _ in anchors)


def test_anchors_use_list_and_code_text_but_not_page_furniture() -> None:
    md = "Intro paragraph long enough.\n\n- first list item here\n- second\n\n```\nprint('hello world')\n```\n"
    content_list = [
        {"type": "text", "text": "Intro paragraph long enough.", "page_idx": 0},
        # Not in the markdown: must not be searched for (a stray hit would
        # skip the real anchors).
        {"type": "page_footnote", "text": "first list item here", "page_idx": 4},
        {"type": "header", "text": "Running header text", "page_idx": 4},
        {"type": "list", "list_items": ["", "first list item here", "second"], "page_idx": 1},
        {"type": "code", "code_body": "print('hello world')", "page_idx": 2},
    ]
    anchors = mineru_result.char_to_page_anchors(content_list, md, 1, 0)
    assert anchors == [
        (0, 1),
        (0, 1),
        # Snapped back over the "- " bullet to the line start.
        (md.index("- first list item"), 2),
        (md.index("print("), 3),
    ]


def test_anchors_skip_short_and_unmatched_text() -> None:
    md = "A heading\n\nBody text that is present."
    content_list = [
        {"type": "text", "text": "Q1", "page_idx": 0},  # too short to place
        {"type": "text", "text": "Re-rendered $x^2$ formula text", "page_idx": 0},  # no match
        {"type": "text", "text": "Body text that is present.", "page_idx": 1},
    ]
    assert mineru_result.char_to_page_anchors(content_list, md, 1, 0) == [
        (0, 1),
        (md.index("Body"), 2),
    ]


# ---------------------------------------------------------------------------
# bucket merge (pdf_parser._merge_buckets over two recorded parses)
# ---------------------------------------------------------------------------


def _bucket(saved: mineru_result.SavedResult, start: int, end: int) -> pdf_parser._BucketResult:
    return pdf_parser._BucketResult(
        saved=saved, page_start=start, page_end=end, elapsed_s=1.0, page_renders=[]
    )


def test_merge_offsets_images_and_anchors_across_buckets(
    full: mineru_result.SavedResult, ranged: mineru_result.SavedResult
) -> None:
    """Merge a 1-page bucket and the 2-3 range as the bucketed path would."""
    page1 = mineru_result.SavedResult(
        markdown=full.markdown[: full.markdown.index("## 2. Segment results")].rstrip(),
        content_list=[b for b in full.content_list if b["page_idx"] == 0],
        root=full.root,
    )
    result = pdf_parser._merge_buckets(
        [_bucket(page1, 1, 1), _bucket(ranged, 2, 3)], FIXTURES / "sample.pdf", 3
    )
    assert result.content == page1.markdown + "\n\n" + ranged.markdown
    assert [(i.page, i.mime) for i in result.images] == [(1, "image/jpeg")]
    assert result.content[result.images[0].position :].startswith("![](images/page_0_chart_3.jpg)")
    assert {b["page"] for b in result.page_layout} == {1, 2, 3}
    starts = {p: o for o, p in result.char_to_page}
    assert result.content[starts[2] :].startswith("## 2. Segment results")
    assert result.content[starts[3] :].startswith("## 3. Outlook")
    assert result.metadata["buckets"] == 2
    assert result.metadata["page_layout_blocks"] == len(page1.content_list) + len(
        ranged.content_list
    )


def test_merge_skips_missing_images_without_failing(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "full", tmp_path / "out")
    shutil.rmtree(tmp_path / "out" / "images")
    saved = mineru_result.load_saved_result(tmp_path / "out")
    result = pdf_parser._merge_buckets([_bucket(saved, 1, 3)], FIXTURES / "sample.pdf", 3)
    assert result.images == []
    assert result.content == saved.markdown


def test_fixture_content_list_is_mineru_v1_shape() -> None:
    """Guard against re-recording with a renderer that changes the contract
    this module depends on (img_path as a relative images/ path, per-mille
    bbox, page_idx)."""
    blocks = json.loads((FIXTURES / "full" / "content_list.json").read_text())
    for b in blocks:
        assert isinstance(b["page_idx"], int)
        assert len(b["bbox"]) == 4
        if "img_path" in b:
            assert b["img_path"].startswith("images/")
