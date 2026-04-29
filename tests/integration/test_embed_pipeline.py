"""Integration: extract → embed_text/embed_image → state=indexed.

Uses the fake embedders (set by the ``env`` fixture).
"""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

from PIL import Image as PILImage
from sqlalchemy import select

from voitta_image_rag.cas import store as cas_store
from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import File, Folder, Image
from voitta_image_rag.services import vector_store
from voitta_image_rag.services.indexing import (
    run_embed_image,
    run_embed_text,
    run_extract,
)


def _png(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    img = PILImage.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_file(folder_root: Path, rel_path: str, content: str | bytes) -> int:
    folder_root.mkdir(parents=True, exist_ok=True)
    abs_path = folder_root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        abs_path.write_text(content)
    else:
        abs_path.write_bytes(content)
    with session_scope() as s:
        folder = s.execute(
            select(Folder).where(Folder.path == str(folder_root))
        ).scalar_one_or_none()
        if folder is None:
            folder = Folder(path=str(folder_root), display_name=folder_root.name)
            s.add(folder)
            s.flush()
        stat = abs_path.stat()
        f = File(
            folder_id=folder.id,
            rel_path=rel_path,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            last_seen_at=int(time.time()),
            state="pending",
        )
        s.add(f)
        s.flush()
        return f.id


async def _run_full_pipeline(file_id: int) -> None:
    await run_extract({"file_id": file_id})
    await run_embed_text({"file_id": file_id})
    with session_scope() as s:
        for img in s.execute(select(Image).where(Image.file_id == file_id)).scalars():
            await run_embed_image({"image_id": img.id})


def test_text_file_pipeline_lands_state_indexed(env: None, tmp_path: Path) -> None:
    init_db()
    file_id = _seed_file(tmp_path / "src", "doc.md", "hello world\n\n" + "para " * 200)

    asyncio.run(_run_full_pipeline(file_id))

    with session_scope() as s:
        f = s.get(File, file_id)
        assert f.state == "indexed"
        assert f.pending_embeds == 0


def test_image_file_pipeline_lands_state_indexed(env: None, tmp_path: Path) -> None:
    init_db()
    file_id = _seed_file(tmp_path / "src", "logo.png", _png())

    asyncio.run(_run_full_pipeline(file_id))

    with session_scope() as s:
        f = s.get(File, file_id)
        assert f.state == "indexed"
        assert f.pending_embeds == 0


def test_embed_text_writes_qdrant_chunks(env: None, tmp_path: Path) -> None:
    init_db()
    file_id = _seed_file(tmp_path / "src", "x.md", "alpha beta gamma\n\n" + "delta " * 200)

    asyncio.run(run_extract({"file_id": file_id}))
    asyncio.run(run_embed_text({"file_id": file_id}))

    client = vector_store.get_client()
    res, _ = client.scroll(vector_store.CHUNKS, limit=100, with_payload=True)
    assert len(res) >= 1
    for p in res:
        assert p.payload["file_id"] == file_id
        assert "text" in p.payload
        assert p.payload["dense_model_version"]
        assert p.payload["sparse_model_version"]


def test_embed_image_dedups_by_cas(env: None, tmp_path: Path) -> None:
    """Two files with identical image bytes share a single Qdrant point."""
    init_db()
    png = _png()
    a_id = _seed_file(tmp_path / "src", "a.png", png)
    b_id = _seed_file(tmp_path / "src", "b.png", png)

    asyncio.run(run_extract({"file_id": a_id}))
    asyncio.run(run_extract({"file_id": b_id}))

    with session_scope() as s:
        images = list(s.execute(select(Image)).scalars())
        assert len(images) == 2
        for img in images:
            asyncio.run(run_embed_image({"image_id": img.id}))

    client = vector_store.get_client()
    res, _ = client.scroll(vector_store.IMAGES, limit=100, with_payload=True)
    assert len(res) == 1  # single point for the shared SHA
    point = res[0]
    assert sorted(point.payload["file_ids"]) == sorted([a_id, b_id])


def test_search_chunks_returns_hit_for_indexed_text(env: None, tmp_path: Path) -> None:
    init_db()
    file_id = _seed_file(tmp_path / "src", "doc.md", "the quick brown fox jumps over")
    asyncio.run(run_extract({"file_id": file_id}))
    asyncio.run(run_embed_text({"file_id": file_id}))

    from voitta_image_rag.services.embedding import (
        get_sparse_embedder,
        get_text_embedder,
    )

    text_emb = get_text_embedder()
    sparse_emb = get_sparse_embedder()
    hits = vector_store.search_chunks(
        dense=text_emb.embed_query("the quick brown fox"),
        sparse=sparse_emb.embed_query("the quick brown fox"),
        limit=5,
    )
    assert len(hits) >= 1
    assert hits[0].payload["file_id"] == file_id


def test_search_images_returns_same_image_top1(env: None, tmp_path: Path) -> None:
    """Reverse image search via the embedder text→image cross-modal path.

    Fake embedders are deterministic: the text "needle" embeds to one vector,
    images embed from their bytes — they don't match. So this test only
    asserts a hit exists and uses the same-image trick: query with the same
    bytes' embedding to verify the dedup-by-cas point is retrievable.
    """
    init_db()
    png = _png()
    file_id = _seed_file(tmp_path / "src", "logo.png", png)
    asyncio.run(run_extract({"file_id": file_id}))
    with session_scope() as s:
        img_id = s.execute(select(Image)).scalar_one().id
    asyncio.run(run_embed_image({"image_id": img_id}))

    from voitta_image_rag.services.embedding import get_image_embedder

    image_emb = get_image_embedder()
    vec = image_emb.embed_image(png)  # query with the same bytes
    hits = vector_store.search_images(vector=vec, limit=5)
    assert len(hits) >= 1
    top = hits[0]
    assert top.payload["image_cas_id"] == cas_store.hash_bytes(png)
    assert top.payload["file_ids"] == [file_id]


def test_pending_embeds_decrement_only_after_both_jobs(env: None, tmp_path: Path) -> None:
    """File with chunks + image: state stays 'extracted' until both embed jobs finish."""
    init_db()
    # A real "doc with embedded image" requires DOCX/PDF; for a deterministic
    # test we cheat by manually adding a chunk + image to a synthesised file.
    folder = tmp_path / "src"
    file_id = _seed_file(folder, "x.png", _png())
    asyncio.run(run_extract({"file_id": file_id}))

    with session_scope() as s:
        f = s.get(File, file_id)
        # Standalone .png → pending_embeds=1 (just the image embed). To exercise
        # the two-job decrement, manually inflate to 2 and verify both decrements
        # land before state flips.
        f.pending_embeds = 2
        f.state = "extracted"
        img_id = s.execute(select(Image)).scalar_one().id

    asyncio.run(run_embed_image({"image_id": img_id}))
    with session_scope() as s:
        assert s.get(File, file_id).state == "extracted"
        assert s.get(File, file_id).pending_embeds == 1

    # Second decrement (simulate embed_text completing on a chunkless file
    # by calling the helper directly).
    from voitta_image_rag.services.indexing import _decrement_pending_embeds

    _decrement_pending_embeds(file_id)
    with session_scope() as s:
        f = s.get(File, file_id)
        assert f.pending_embeds == 0
        assert f.state == "indexed"
