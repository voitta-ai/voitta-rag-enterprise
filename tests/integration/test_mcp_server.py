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

from voitta_image_rag.cas import store as cas_store
from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import (
    Chunk,
    ChunkImageLink,
    File,
    Folder,
    Image,
)
from voitta_image_rag.mcp_server import (
    get_chunk_images,
    get_chunk_range,
    get_file,
    get_image,
    list_indexed_folders,
    resolve_url,
    search,
    search_images,
)
from voitta_image_rag.services.indexing import (
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
        '{"a.md": "https://docs.example/page"}'
    )
    (src / "a.md").write_text("alpha")
    (src / "b.md").write_text("beta")

    from voitta_image_rag.services.scanner import scan_folder

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
