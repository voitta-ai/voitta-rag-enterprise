"""Tests for the folder-stats compute + publish helpers.

Two responsibilities:
* :func:`compute_folder_stats` returns a JSON-able dict with the same
  shape the REST endpoint ships.
* :func:`publish_folder_stats` emits one ``folder.stats_changed`` event
  on the ``folders`` topic carrying that dict, and silently no-ops when
  the folder row is missing.
"""

from __future__ import annotations

import asyncio

import pytest

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Folder, Image
from voitta_rag_enterprise.services import events
from voitta_rag_enterprise.services.folder_stats import (
    compute_folder_stats,
    publish_folder_stats,
)


def _seed(folder_path: str = "/tmp/x", display: str = "x") -> int:
    init_db()
    with session_scope() as s:
        folder = Folder(path=folder_path, display_name=display)
        s.add(folder)
        s.flush()
        return folder.id


def _add_file(
    folder_id: int,
    rel_path: str,
    *,
    state: str = "indexed",
    size: int = 100,
    chunks: int = 0,
    images: int = 0,
    source_url: str | None = None,
    source_meta: str | None = None,
) -> int:
    with session_scope() as s:
        f = File(
            folder_id=folder_id,
            rel_path=rel_path,
            size_bytes=size,
            mtime_ns=0,
            last_seen_at=0,
            state=state,
            source_url=source_url,
            source_meta=source_meta,
        )
        s.add(f)
        s.flush()
        for i in range(chunks):
            s.add(
                Chunk(
                    file_id=f.id,
                    chunk_index=i,
                    chunk_hash=f"{f.id}-{i}",
                    text="body",
                    char_start=0,
                    char_end=4,
                    created_at=0,
                )
            )
        for i in range(images):
            s.add(
                Image(
                    file_id=f.id,
                    image_index=i,
                    image_cas_id=f"cas-{f.id}-{i}",
                    page=1,
                    width=10,
                    height=10,
                    mime="image/png",
                    kind="figure",
                    created_at=0,
                )
            )
        return f.id


# ---------------------------------------------------------------------------
# compute_folder_stats
# ---------------------------------------------------------------------------


def test_compute_aggregates_counts_and_extension_breakdown(env: None) -> None:
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=3, size=100)
    _add_file(fid, "b.md", state="indexed", chunks=2, size=200)
    _add_file(fid, "c.pdf", state="error", size=300)
    _add_file(fid, "d.png", state="unsupported", size=50)
    _add_file(fid, "e.md", state="extracted", size=20)
    _add_file(fid, "f.md", state="pending", size=10)

    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder)

    assert out["folder_id"] == fid
    assert out["files_total"] == 6
    assert out["files_indexed"] == 2
    assert out["files_error"] == 1
    assert out["files_unsupported"] == 1
    assert out["files_in_progress"] == 1
    assert out["files_pending"] == 1
    assert out["chunks_total"] == 5
    assert out["bytes_total"] == 100 + 200 + 300 + 50 + 20 + 10

    by_ext = out["by_extension"]
    assert by_ext[".md"]["files"] == 4  # a/b/e/f
    assert by_ext[".md"]["indexed"] == 2
    assert by_ext[".md"]["chunks"] == 5
    assert by_ext[".md"]["pending"] == 1
    assert by_ext[".md"]["in_progress"] == 1
    assert by_ext[".pdf"]["files"] == 1
    assert by_ext[".pdf"]["error"] == 1


def test_compute_buckets_google_workspace_files_by_source_url(env: None) -> None:
    """Drive sync exports Google Docs/Sheets/Slides/Forms as .md files;
    grouping them by extension lumps them under a generic '.md' bucket
    that hides the real types from the user. We classify by the
    ``source_url`` prefix so the sidebar shows 'Google Doc' / 'Google
    Sheet' as first-class buckets, with plain ``.md`` reserved for
    actually-on-disk markdown."""
    fid = _seed()
    # Two real Google Docs exported as markdown.
    _add_file(
        fid, "Project/01-Intro.md", state="indexed", chunks=4,
        source_url="https://docs.google.com/document/d/abc/edit",
    )
    _add_file(
        fid, "Project/02-Plan.md", state="indexed", chunks=6,
        source_url="https://docs.google.com/document/d/abc/edit#tab=t.1",
    )
    # A Google Sheet → per-sheet markdowns, each carries the same prefix.
    _add_file(
        fid, "Q3 Forecast/01-Sales.md", state="indexed", chunks=3,
        source_url="https://docs.google.com/spreadsheets/d/xyz/edit",
    )
    # A Slides deck and a Form.
    _add_file(
        fid, "Pitch/01-Pitch.md", state="pending",
        source_url="https://docs.google.com/presentation/d/123/edit",
    )
    _add_file(
        fid, "Survey.md", state="indexed", chunks=1,
        source_url="https://docs.google.com/forms/d/abc/edit",
    )
    # A vanilla markdown file (no source_url) — must stay under '.md'.
    _add_file(fid, "README.md", state="indexed", chunks=2)

    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder)
    by_ext = out["by_extension"]

    assert by_ext["Google Doc"]["files"] == 2
    assert by_ext["Google Doc"]["chunks"] == 10
    assert by_ext["Google Sheet"]["files"] == 1
    assert by_ext["Google Slides"]["files"] == 1
    assert by_ext["Google Slides"]["pending"] == 1
    assert by_ext["Google Form"]["files"] == 1
    # The real markdown didn't get swept into a workspace bucket.
    assert by_ext[".md"]["files"] == 1
    assert by_ext[".md"]["chunks"] == 2


def test_compute_handles_no_extension_files(env: None) -> None:
    fid = _seed()
    _add_file(fid, "Makefile", state="indexed", chunks=1)
    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder)
    assert "(no ext)" in out["by_extension"]
    assert out["by_extension"]["(no ext)"]["files"] == 1


def test_compute_excludes_deleted_files(env: None) -> None:
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=2)
    _add_file(fid, "ghost.md", state="deleted")
    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder)
    assert out["files_total"] == 1


def test_compute_counts_unique_images(env: None) -> None:
    fid = _seed()
    f1 = _add_file(fid, "a.md", state="indexed")
    f2 = _add_file(fid, "b.md", state="indexed")
    # Manually add two images to f1 sharing the same SHA + one unique
    # image to f2 — distinct count should be 2.
    with session_scope() as s:
        for i in range(2):
            s.add(
                Image(
                    file_id=f1,
                    image_index=10 + i,
                    image_cas_id="shared-sha",
                    page=1,
                    width=10,
                    height=10,
                    mime="image/png",
                    kind="figure",
                    created_at=0,
                )
            )
        s.add(
            Image(
                file_id=f2,
                image_index=0,
                image_cas_id="other-sha",
                page=1,
                width=10,
                height=10,
                mime="image/png",
                kind="figure",
                created_at=0,
            )
        )
    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder)
    assert out["images_total"] == 3
    assert out["images_unique"] == 2


# ---------------------------------------------------------------------------
# publish_folder_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_emits_folder_stats_changed(env: None) -> None:
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=2)

    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["folders"]) as sub:
            with session_scope() as s:
                publish_folder_stats(s, fid)
            await sub.wait(timeout=1.0)
            (event,) = sub.drain()
            assert event["type"] == "folder.stats_changed"
            assert event["folder_id"] == fid
            assert event["stats"]["chunks_total"] == 2
            assert event["stats"]["files_indexed"] == 1
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_publish_is_noop_when_folder_missing(env: None) -> None:
    init_db()
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["folders"]) as sub:
            with session_scope() as s:
                publish_folder_stats(s, 999_999)  # never existed
            assert await sub.wait(timeout=0.1) is False
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_publish_swallows_compute_errors(
    env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside compute_folder_stats must NOT raise back into the
    indexer's hot path. We log + carry on."""
    fid = _seed()
    _add_file(fid, "a.md", state="indexed")

    def _boom(*a, **k):
        raise RuntimeError("boom")

    import voitta_rag_enterprise.services.folder_stats as svc

    monkeypatch.setattr(svc, "compute_folder_stats", _boom)
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["folders"]) as sub:
            with session_scope() as s:
                publish_folder_stats(s, fid)  # must not raise
            assert await sub.wait(timeout=0.1) is False
    finally:
        events.uninstall_loop()

