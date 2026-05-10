"""``ChunkInfo`` / ``ImageInfo`` JSON shape — drop empty PDF-only fields.

The layout/page/nearby-image fields exist on the response models so PDF
search results carry them, but every other parser leaves them empty.
We exclude them from ``model_dump`` output so a markdown / code / docx
chunk's response is clean.
"""

from __future__ import annotations

from voitta_rag_enterprise.mcp_server import ChunkInfo, ImageInfo


def _chunk(**overrides) -> ChunkInfo:
    base = {
        "chunk_id": 1,
        "file_id": 7,
        "file_path": "src/foo.py",
        "chunk_index": 0,
        "text": "def foo(): ...",
        "score": 0.9,
    }
    base.update(overrides)
    return ChunkInfo(**base)


def _image(**overrides) -> ImageInfo:
    base = {
        "image_id": 11,
        "file_id": 7,
        "file_path": "report.pdf",
        "image_cas_id": "abc123",
        "page": None,
        "width": None,
        "height": None,
        "mime": None,
        "score": 0.7,
    }
    base.update(overrides)
    return ImageInfo(**base)


# ---------------------------------------------------------------------------
# ChunkInfo
# ---------------------------------------------------------------------------


def test_chunk_dump_drops_empty_pdf_fields() -> None:
    """A code/markdown chunk hit ships only the universally meaningful keys."""
    d = _chunk().model_dump()
    assert set(d.keys()) == {
        "chunk_id", "file_id", "file_path", "chunk_index", "text", "score",
    }


def test_chunk_dump_keeps_fields_when_set() -> None:
    """A PDF-sourced chunk hit ships everything."""
    d = _chunk(
        nearby_image_ids=[3, 5],
        page=2,
        pages=[2, 3],
        layout={"layout_kind": "page", "layout_has_image": True},
    ).model_dump()
    assert d["nearby_image_ids"] == [3, 5]
    assert d["page"] == 2
    assert d["pages"] == [2, 3]
    assert d["layout"]["layout_kind"] == "page"


def test_chunk_dump_drops_empty_lists_individually() -> None:
    """Mixed: ``page`` is set but ``nearby_image_ids`` is empty — keep page,
    drop the empty list."""
    d = _chunk(page=4).model_dump()
    assert d.get("page") == 4
    assert "nearby_image_ids" not in d
    assert "pages" not in d
    assert "layout" not in d


def test_chunk_dump_keeps_score_zero() -> None:
    """``score=0.0`` is meaningful (it's a real similarity), not a missing
    value — must not be stripped."""
    d = _chunk(score=0.0).model_dump()
    assert "score" in d
    assert d["score"] == 0.0


def test_chunk_json_round_trip() -> None:
    """``model_dump_json`` honours the serializer too."""
    import json

    js = _chunk().model_dump_json()
    parsed = json.loads(js)
    assert "page" not in parsed
    assert "layout" not in parsed
    assert parsed["chunk_id"] == 1


# ---------------------------------------------------------------------------
# ImageInfo
# ---------------------------------------------------------------------------


def test_image_dump_drops_empty_pdf_fields() -> None:
    d = _image().model_dump()
    assert set(d.keys()) == {
        "image_id", "file_id", "file_path", "image_cas_id", "kind", "score",
    }


def test_image_dump_keeps_fields_when_set() -> None:
    d = _image(
        page=3,
        width=1024,
        height=768,
        mime="image/png",
        layout={"layout_kind": "figure"},
    ).model_dump()
    assert d["page"] == 3
    assert d["width"] == 1024
    assert d["height"] == 768
    assert d["mime"] == "image/png"
    assert d["layout"]["layout_kind"] == "figure"


def test_image_kind_default_is_figure() -> None:
    """``kind`` is required-ish (has default 'figure') — present in the dump
    so clients can rely on it without conditional access."""
    d = _image().model_dump()
    assert d["kind"] == "figure"
