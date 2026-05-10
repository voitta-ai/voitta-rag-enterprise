"""MCP response models — every declared field appears on the wire.

The previous version of these models stripped null/empty PDF-specific
fields from ``model_dump`` while leaving the JSONSchema marking them
required. FastMCP clients running structured-content validation rejected
those responses (``'page' is a required property``).

The new policy is the simplest one that matches the wire format to the
schema:

* Every optional field carries a default (``None`` for scalars,
  ``Field(default_factory=...)`` for collections).
* ``model_dump`` emits every declared field, including ``null`` /
  ``[]`` when no value is set.
* The advertised JSONSchema marks only the truly-mandatory fields as
  required; every optional one is "not required, may be null/empty".

These tests pin both halves of that contract so a future "trim the
response" refactor can't silently break MCP clients again.
"""

from __future__ import annotations

import json

from voitta_rag_enterprise.mcp_server import (
    ChunkInfo,
    FileInfo,
    ImageInfo,
    PageImageInfo,
)


# ---------------------------------------------------------------------------
# Wire format: every declared field present, with default value if unset
# ---------------------------------------------------------------------------


def test_chunkinfo_emits_every_field_with_defaults() -> None:
    d = ChunkInfo(
        chunk_id=1, file_id=7, file_path="src/foo.py", chunk_index=0,
        text="def foo(): ...",
    ).model_dump()
    assert set(d.keys()) == {
        "chunk_id", "file_id", "file_path", "chunk_index", "text",
        "nearby_image_ids", "score", "page", "pages", "layout",
        "source_url", "source_kind",
    }
    # Defaults: empty list for collections, None for scalars, "other"
    # for the source_kind classifier when no provenance was passed in.
    assert d["nearby_image_ids"] == []
    assert d["pages"] == []
    assert d["score"] is None
    assert d["page"] is None
    assert d["layout"] is None
    assert d["source_url"] is None
    assert d["source_kind"] == "other"


def test_chunkinfo_keeps_real_values() -> None:
    d = ChunkInfo(
        chunk_id=1, file_id=7, file_path="report.pdf", chunk_index=0, text="x",
        nearby_image_ids=[3, 5], page=2, pages=[2, 3],
        layout={"layout_kind": "page", "layout_has_image": True},
        score=0.0,
    ).model_dump()
    assert d["nearby_image_ids"] == [3, 5]
    assert d["page"] == 2
    assert d["pages"] == [2, 3]
    assert d["layout"]["layout_kind"] == "page"
    # score=0.0 is meaningful (real similarity), not "missing".
    assert d["score"] == 0.0


def test_imageinfo_emits_every_field_with_defaults() -> None:
    d = ImageInfo(
        image_id=11, file_id=7, file_path="report.pdf", image_cas_id="abc123",
    ).model_dump()
    assert set(d.keys()) == {
        "image_id", "file_id", "file_path", "image_cas_id",
        "page", "width", "height", "mime", "kind", "score", "layout",
        "source_url", "source_kind",
    }
    assert d["kind"] == "figure"  # explicit default
    assert d["source_kind"] == "other"
    for k in ("page", "width", "height", "mime", "score", "layout", "source_url"):
        assert d[k] is None


def test_imageinfo_keeps_real_values() -> None:
    d = ImageInfo(
        image_id=11, file_id=7, file_path="report.pdf", image_cas_id="abc",
        page=3, width=1024, height=768, mime="image/png",
        layout={"layout_kind": "figure"},
    ).model_dump()
    assert d["page"] == 3
    assert d["width"] == 1024
    assert d["height"] == 768
    assert d["mime"] == "image/png"
    assert d["layout"]["layout_kind"] == "figure"


def test_fileinfo_emits_every_field_with_defaults() -> None:
    d = FileInfo(id=1, folder_id=2, rel_path="a.md", state="indexed").model_dump()
    assert set(d.keys()) == {
        "id", "folder_id", "rel_path", "state",
        "source_url", "last_indexed_at", "source_kind",
    }
    assert d["source_url"] is None
    assert d["last_indexed_at"] is None
    assert d["source_kind"] == "other"


def test_pageimageinfo_emits_every_field_with_defaults() -> None:
    d = PageImageInfo(image_id=1, file_id=2, page=1).model_dump()
    assert set(d.keys()) == {
        "image_id", "file_id", "page", "width", "height", "mime",
        "source_url", "source_kind",
    }
    assert d["source_kind"] == "other"
    for k in ("width", "height", "mime", "source_url"):
        assert d[k] is None


# ---------------------------------------------------------------------------
# Schema agreement: required-list matches the truly-mandatory fields
# ---------------------------------------------------------------------------


def _required(model) -> set[str]:
    return set(model.model_json_schema().get("required", []))


def test_chunkinfo_schema_required_is_minimal() -> None:
    assert _required(ChunkInfo) == {
        "chunk_id", "file_id", "file_path", "chunk_index", "text",
    }


def test_imageinfo_schema_required_is_minimal() -> None:
    assert _required(ImageInfo) == {
        "image_id", "file_id", "file_path", "image_cas_id",
    }


def test_fileinfo_schema_required_is_minimal() -> None:
    assert _required(FileInfo) == {"id", "folder_id", "rel_path", "state"}


def test_pageimageinfo_schema_required_is_minimal() -> None:
    assert _required(PageImageInfo) == {"image_id", "file_id", "page"}


# ---------------------------------------------------------------------------
# Wire ↔ schema parity (the actual regression we just fixed)
# ---------------------------------------------------------------------------


def test_imageinfo_wire_includes_every_required_schema_field() -> None:
    """The MCP client validator complained when ``page`` (required in
    schema) was missing from the wire payload. This was caused by a
    null-stripping serializer + required-without-default schema. Verify
    the two halves agree."""
    schema_required = _required(ImageInfo)
    wire_keys = set(ImageInfo(
        image_id=1, file_id=2, file_path="x.png", image_cas_id="abc",
    ).model_dump().keys())
    missing = schema_required - wire_keys
    assert not missing, f"schema requires {missing} but wire omits them"


def test_chunkinfo_wire_includes_every_required_schema_field() -> None:
    schema_required = _required(ChunkInfo)
    wire_keys = set(ChunkInfo(
        chunk_id=1, file_id=2, file_path="x.md", chunk_index=0, text="hi",
    ).model_dump().keys())
    assert not (schema_required - wire_keys)


def test_chunkinfo_json_round_trip_preserves_null_fields() -> None:
    """JSON dump emits ``null`` for unset scalars and ``[]`` for unset
    collections — no field disappears en route."""
    js = ChunkInfo(
        chunk_id=1, file_id=2, file_path="x.md", chunk_index=0, text="hi",
    ).model_dump_json()
    parsed = json.loads(js)
    assert parsed["page"] is None
    assert parsed["layout"] is None
    assert parsed["nearby_image_ids"] == []
    assert parsed["pages"] == []
