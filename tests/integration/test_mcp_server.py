"""Integration tests for the MCP server tools.

We exercise the underlying tool functions directly. FastMCP-over-HTTP needs
a running ASGI app + transport layer; covering it here would just retest
fastmcp's plumbing without exercising our code paths.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image as PILImage
from sqlalchemy import select

from voitta_rag_enterprise.cas import store as cas_store
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import (
    Chunk,
    ChunkImageLink,
    File,
    Folder,
    Image,
)
from voitta_rag_enterprise.mcp_server import (
    get_chunk_images,
    get_chunk_range,
    get_file,
    get_image,
    get_workbook,
    list_indexed_folders,
    list_page_images,
    resolve_url,
    search,
    search_images,
)
from voitta_rag_enterprise.services.indexing import (
    run_embed_image,
    run_embed_text,
    run_extract,
)


def _png() -> bytes:
    img = PILImage.new("RGB", (8, 8), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_and_index(root: Path, layout: dict[str, str | bytes]) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
    init_db()
    with session_scope() as s:
        folder = s.execute(
            select(Folder).where(Folder.path == str(root))
        ).scalar_one_or_none()
        if folder is None:
            folder = Folder(path=str(root), display_name=root.name)
            s.add(folder)
            s.flush()
        folder_id = folder.id
        for rel in layout:
            stat = (root / rel).stat()
            f = File(
                folder_id=folder_id,
                rel_path=rel,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                last_seen_at=0,
                state="pending",
            )
            s.add(f)
    with session_scope() as s:
        ids = [
            f.id for f in s.execute(select(File).where(File.folder_id == folder_id)).scalars()
        ]
    for fid in ids:
        asyncio.run(run_extract({"file_id": fid}))
        asyncio.run(run_embed_text({"file_id": fid}))
        with session_scope() as s:
            for img in s.execute(select(Image).where(Image.file_id == fid)).scalars():
                asyncio.run(run_embed_image({"image_id": img.id}))
    return folder_id


def test_list_indexed_folders_counts(env: None, tmp_path: Path) -> None:
    _seed_and_index(tmp_path / "src", {"a.md": "alpha beta", "b.md": "gamma delta"})
    folders = list_indexed_folders()
    assert len(folders) == 1
    f = folders[0]
    assert f.files_total == 2
    assert f.files_indexed == 2
    assert f.source_type == "filesystem"


def test_search_returns_hits(env: None, tmp_path: Path) -> None:
    _seed_and_index(tmp_path / "src", {"doc.md": "the quick brown fox jumps over"})
    results = search("quick brown")
    assert len(results) >= 1
    top = results[0]
    assert "fox" in top.text or "brown" in top.text
    assert top.file_path == "doc.md"
    assert top.score is not None


def test_search_folder_filter(env: None, tmp_path: Path) -> None:
    a = _seed_and_index(tmp_path / "a", {"x.md": "alpha"})
    _seed_and_index(tmp_path / "b", {"y.md": "alpha"})
    results = search("alpha", folder_ids=[a])
    assert all(r.file_path == "x.md" for r in results)


def test_search_images_round_trip(env: None, tmp_path: Path) -> None:
    _seed_and_index(tmp_path / "src", {"logo.png": _png()})
    results = search_images("any query")
    assert len(results) >= 1
    top = results[0]
    assert top.image_cas_id == cas_store.hash_bytes(_png())
    assert top.file_path == "logo.png"


def test_get_file_returns_text(env: None, tmp_path: Path) -> None:
    _seed_and_index(tmp_path / "src", {"d.md": "# Title\n\nbody text"})
    with session_scope() as s:
        fid = s.execute(select(File)).scalar_one().id
    out = get_file(fid)
    assert "Title" in out["text"]
    assert out["file"]["state"] == "indexed"


def test_get_file_unknown_raises(env: None) -> None:
    init_db()
    try:
        get_file(999)
    except ValueError as e:
        assert "999" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_get_chunk_range_orders_by_index(env: None, tmp_path: Path) -> None:
    _seed_and_index(
        tmp_path / "src",
        {"big.md": "alpha\n\n" + ("beta\n\n" * 800) + "gamma"},
    )
    with session_scope() as s:
        fid = s.execute(select(File)).scalar_one().id
        total = s.execute(select(Chunk).where(Chunk.file_id == fid)).scalars().all()
    assert len(total) > 1
    chunks = get_chunk_range(fid, 0, 2)
    assert [c.chunk_index for c in chunks] == [0, 1]


def test_get_chunk_range_clamps_empty_range(env: None, tmp_path: Path) -> None:
    _seed_and_index(tmp_path / "src", {"d.md": "alpha"})
    with session_scope() as s:
        fid = s.execute(select(File)).scalar_one().id
    assert get_chunk_range(fid, 5, 5) == []


def test_get_chunk_images_returns_links(env: None, tmp_path: Path) -> None:
    _seed_and_index(tmp_path / "src", {"d.md": "hello world"})
    # No images in markdown — manually insert a link to verify the join.
    with session_scope() as s:
        f = s.execute(select(File)).scalar_one()
        chunk = s.execute(select(Chunk).where(Chunk.file_id == f.id)).scalar_one()
        cas_store.write_image_blob(_png())
        img = Image(
            file_id=f.id,
            image_index=0,
            image_cas_id=cas_store.hash_bytes(_png()),
            anchor_chunk=0,
            mime="image/png",
            width=8,
            height=8,
        )
        s.add(img)
        s.flush()
        s.add(ChunkImageLink(chunk_id=chunk.id, image_id=img.id, distance=0))
        chunk_id = chunk.id

    images = get_chunk_images(chunk_id)
    assert len(images) == 1
    assert images[0].mime == "image/png"
    assert images[0].score == 0.0


def test_get_image_returns_base64(env: None, tmp_path: Path) -> None:
    _seed_and_index(tmp_path / "src", {"logo.png": _png()})
    with session_scope() as s:
        img_id = s.execute(select(Image)).scalar_one().id
    out = get_image(img_id)
    import base64

    assert out["mime"] == "image/png"
    assert base64.b64decode(out["data_base64"]) == _png()


def test_resolve_url_exact_and_prefix(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    src.mkdir()
    (src / ".voitta_sources.json").write_text(
        '{"a.md": {"url": "https://docs.example/page"}}'
    )
    (src / "a.md").write_text("alpha")
    (src / "b.md").write_text("beta")

    from voitta_rag_enterprise.services.scanner import scan_folder

    with session_scope() as s:
        folder = Folder(path=str(src), display_name="src")
        s.add(folder)
        s.flush()
        scan_folder(s, folder)

    exact = resolve_url("https://docs.example/page")
    assert len(exact) == 1
    assert exact[0].rel_path == "a.md"

    fragment = resolve_url("https://docs.example/page#section-2")
    assert len(fragment) == 1  # falls back to prefix match
    assert fragment[0].rel_path == "a.md"

    miss = resolve_url("https://nope.example")
    assert miss == []


import base64

import pytest


def test_get_workbook_returns_xlsx_bytes(env: None, tmp_path: Path) -> None:
    """A per-sheet markdown file → workbook lookup → base64 xlsx bytes."""
    init_db()
    src = tmp_path / "src"
    src.mkdir()
    # Lay out the on-disk shape SpreadsheetExporter produces.
    md_rel = "MyDir/Q4 Plan/01-Sales.md"
    md_path = src / md_rel
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# Sales\n\n| Region | Q4 |\n")
    xlsx_path = src / ".voitta_workbooks" / "MyDir" / "Q4 Plan.xlsx"
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path.write_bytes(b"XLSX-BYTES")
    with session_scope() as s:
        folder = Folder(path=str(src), display_name="src")
        s.add(folder)
        s.flush()
        f = File(folder_id=folder.id, rel_path=md_rel, state="indexed", last_seen_at=0)
        s.add(f)
        s.flush()
        fid = f.id

    out = get_workbook(fid)
    assert out["filename"] == "Q4 Plan.xlsx"
    assert out["mime"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert base64.b64decode(out["data_base64"]) == b"XLSX-BYTES"
    assert out["size_bytes"] == len(b"XLSX-BYTES")


def test_get_workbook_404_when_xlsx_absent(env: None, tmp_path: Path) -> None:
    """A markdown file without an accompanying xlsx (older sync) errors
    with a clear message rather than silently returning empty bytes."""
    init_db()
    src = tmp_path / "src"
    src.mkdir()
    md_path = src / "Q4/01-Sales.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("body")
    with session_scope() as s:
        folder = Folder(path=str(src), display_name="src")
        s.add(folder)
        s.flush()
        f = File(folder_id=folder.id, rel_path="Q4/01-Sales.md", state="indexed", last_seen_at=0)
        s.add(f)
        s.flush()
        fid = f.id
    with pytest.raises(FileNotFoundError, match="Workbook xlsx not found"):
        get_workbook(fid)


def test_get_workbook_rejects_non_md_file(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.png").write_bytes(b"\x89PNG")
    with session_scope() as s:
        folder = Folder(path=str(src), display_name="src")
        s.add(folder)
        s.flush()
        f = File(folder_id=folder.id, rel_path="x.png", state="indexed", last_seen_at=0)
        s.add(f)
        s.flush()
        fid = f.id
    with pytest.raises(ValueError, match="not a Sheets-derived markdown"):
        get_workbook(fid)


def test_get_workbook_unknown_file_id_raises(env: None) -> None:
    init_db()
    with pytest.raises(ValueError, match="not found"):
        get_workbook(99999)


def test_get_file_pre_extraction_returns_empty_text(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("hi")
    with session_scope() as s:
        folder = Folder(path=str(src), display_name="src")
        s.add(folder)
        s.flush()
        f = File(folder_id=folder.id, rel_path="a.md", state="pending", last_seen_at=0)
        s.add(f)
        s.flush()
        fid = f.id
    out = get_file(fid)
    assert out["text"] == ""
    assert out["file"]["state"] == "pending"


# ---------------------------------------------------------------------------
# source_kind / source_url plumbing through every response shape
# ---------------------------------------------------------------------------


def test_get_file_carries_source_kind_for_workspace(env: None, tmp_path: Path) -> None:
    """A Drive-synced Google Doc gets ``source_kind='google_doc'`` so an
    LLM can branch on what kind of source it's looking at."""
    _seed_and_index(tmp_path / "drive", {"Project/01-Intro.md": "alpha"})
    with session_scope() as s:
        f = s.execute(select(File)).scalar_one()
        f.source_url = "https://docs.google.com/document/d/abc/edit"
        s.commit()
        fid = f.id
    out = get_file(fid)
    assert out["file"]["source_kind"] == "google_doc"
    assert out["file"]["source_url"].startswith("https://docs.google.com/document/")


def test_search_hits_carry_file_provenance(env: None, tmp_path: Path) -> None:
    """Every chunk hit ships ``source_url`` + ``source_kind`` so the LLM
    doesn't need a follow-up ``get_file`` to deep-link to the Google
    doc."""
    _seed_and_index(tmp_path / "drive", {"Project/01-Intro.md": "alpha beta gamma"})
    with session_scope() as s:
        f = s.execute(select(File)).scalar_one()
        f.source_url = "https://docs.google.com/document/d/abc/edit"
        s.commit()
    hits = search("alpha")
    assert hits
    h = hits[0]
    assert h.source_kind == "google_doc"
    assert h.source_url == "https://docs.google.com/document/d/abc/edit"


# ---------------------------------------------------------------------------
# Cross-file image resolution for Google Drive exports
# ---------------------------------------------------------------------------


def test_get_chunk_images_resolves_cross_file_markdown_refs(
    env: None, tmp_path: Path,
) -> None:
    """The Drive connector writes a slide thumbnail as a sibling File
    row, referenced from the slide's markdown as ``![](images/x.png)``.
    The intra-file linker can't see across File rows, so this used to
    return ``[]``. With the cross-file resolver, the slide's chunk
    surfaces the thumbnail Image row."""
    folder_id = _seed_and_index(
        tmp_path / "deck",
        {
            "Pitch/12-Slide 12.md": "# Slide 12\n\n![](images/slide_12.png)\n\nbody",
            "Pitch/images/slide_12.png": _png(),
        },
    )

    # Resolve ids. The slide markdown and its thumbnail image File row
    # were both indexed in the seed pass.
    with session_scope() as s:
        chunk = s.execute(
            select(Chunk).join(File, Chunk.file_id == File.id)
            .where(File.rel_path == "Pitch/12-Slide 12.md")
        ).scalar_one()
        thumbnail_img = s.execute(
            select(Image).join(File, Image.file_id == File.id)
            .where(File.rel_path == "Pitch/images/slide_12.png")
        ).scalar_one()
        chunk_id = chunk.id
        thumbnail_image_id = thumbnail_img.id

    images = get_chunk_images(chunk_id)
    # The slide thumbnail must surface despite living in a different
    # File row from the chunk.
    image_ids = {i.image_id for i in images}
    assert thumbnail_image_id in image_ids
    # Cross-file refs are stamped score=0.0 ("direct reference") to
    # distinguish them from intra-file links that carry chunk distance.
    resolved = next(i for i in images if i.image_id == thumbnail_image_id)
    assert resolved.score == 0.0
    # And the resolved entry knows it points at the *other* file.
    assert resolved.file_path == "Pitch/images/slide_12.png"


def test_get_chunk_images_skips_remote_urls(env: None, tmp_path: Path) -> None:
    """``![](https://example.com/foo.png)`` must not trigger a DB
    lookup; only sibling-local refs resolve."""
    _seed_and_index(
        tmp_path / "deck",
        {"a.md": "# Title\n\n![alt](https://example.com/foo.png)"},
    )
    with session_scope() as s:
        chunk = s.execute(select(Chunk)).scalar_one()
        chunk_id = chunk.id
    assert get_chunk_images(chunk_id) == []


def test_get_chunk_images_handles_dotdot_safely(env: None, tmp_path: Path) -> None:
    """A markdown ref escaping the folder root must not resolve. We
    normalize ``..`` and reject any path that walks above the file's
    parent dir."""
    _seed_and_index(
        tmp_path / "deck",
        {"a/b/c.md": "![](../../../etc/passwd)"},
    )
    with session_scope() as s:
        chunk = s.execute(select(Chunk)).scalar_one()
        chunk_id = chunk.id
    # No match: even if the path normalized cleanly, no such File row
    # exists. The contract is "no exception, empty result".
    assert get_chunk_images(chunk_id) == []


# ---------------------------------------------------------------------------
# File-level image-ref resolution (the "chunks-without-the-image-line" gap)
# ---------------------------------------------------------------------------


def test_get_chunk_images_falls_back_to_file_level_refs(
    env: None, tmp_path: Path,
) -> None:
    """A Google Doc exports as one markdown file with N chunks, but only
    the chunks that contain a ``![](images/X.png)`` line resolve via the
    text-scan path. The intermediate chunks must still surface the doc's
    inline images via the file-level fallback so an LLM reading chunk
    #15 of a 21-chunk doc isn't told 'no figures here'."""
    folder_id = _seed_and_index(
        tmp_path / "drive",
        {
            # Use a body long enough to split into multiple chunks. The
            # default chunker is char-based; we don't depend on the exact
            # cut as long as the body chunk has no '![](' token.
            "Doc/01-Intro.md": (
                "![](images/figure_a.png)\n\n"
                + ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 200)
            ),
            "Doc/images/figure_a.png": _png(),
        },
    )

    with session_scope() as s:
        # Find a chunk in the markdown that does NOT contain ``![](``.
        body_chunk = s.execute(
            select(Chunk)
            .join(File, Chunk.file_id == File.id)
            .where(File.rel_path == "Doc/01-Intro.md")
            .where(~Chunk.text.like("%![](%"))
        ).scalars().first()
        assert body_chunk is not None, "expected a body chunk without an image ref"
        body_chunk_id = body_chunk.id
        # And the target image's id.
        figure_img = s.execute(
            select(Image).join(File, Image.file_id == File.id)
            .where(File.rel_path == "Doc/images/figure_a.png")
        ).scalar_one()
        figure_image_id = figure_img.id

    images = get_chunk_images(body_chunk_id)
    assert images, "body chunk should surface the file's figures via fallback"
    # The fallback marks file-level results with score=None (vs 0.0 for
    # direct in-chunk refs, vs float for intra-file linker hits).
    surfaced = [i for i in images if i.image_id == figure_image_id]
    assert surfaced
    assert surfaced[0].score is None


def test_list_page_images_falls_back_for_workspace_files(
    env: None, tmp_path: Path,
) -> None:
    """A Slides slide file has no ``kind='page_render'`` Image rows (those
    only come from the PDF pipeline), yet the slide's thumbnail IS in
    the index as a sibling File row. list_page_images now surfaces those
    cross-file refs so an LLM asking for the slide's visual gets the
    thumbnail back."""
    _seed_and_index(
        tmp_path / "deck",
        {
            "Pitch/01-Slide 1.md": "# Slide 1\n\n![](images/slide_1.png)",
            "Pitch/images/slide_1.png": _png(),
        },
    )
    with session_scope() as s:
        slide = s.execute(
            select(File).where(File.rel_path == "Pitch/01-Slide 1.md")
        ).scalar_one()
        thumb = s.execute(
            select(File).where(File.rel_path == "Pitch/images/slide_1.png")
        ).scalar_one()
        slide_file_id = slide.id
        thumb_image_id = s.execute(
            select(Image).where(Image.file_id == thumb.id)
        ).scalar_one().id

    pages = list_page_images(slide_file_id)
    assert len(pages) == 1
    p = pages[0]
    assert p.image_id == thumb_image_id
    # Synthetic 1-indexed appearance order — first ref in the markdown.
    assert p.page == 1
    # Carries the target's source_kind (here, an .image — no source_url
    # is set on the PNG by this test helper, so source_kind falls back
    # to the extension classifier).
    assert p.source_kind == "image"


def test_list_page_images_returns_empty_for_textonly_markdown(
    env: None, tmp_path: Path,
) -> None:
    """A markdown file with no image references and no page renders
    returns an empty list — the fallback resolver must not invent
    matches."""
    _seed_and_index(tmp_path / "src", {"plain.md": "just words, no images"})
    with session_scope() as s:
        fid = s.execute(select(File)).scalar_one().id
    assert list_page_images(fid) == []
