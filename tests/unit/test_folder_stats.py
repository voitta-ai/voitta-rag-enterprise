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
import contextlib

import pytest

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Folder, Image
from voitta_rag_enterprise.services import events
from voitta_rag_enterprise.services import folder_stats as fs_mod
from voitta_rag_enterprise.services.folder_stats import (
    compute_folder_stats,
    mark_folder_stats_dirty,
    publish_folder_stats,
    run_stats_flusher,
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
    error: str | None = None,
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
            error=error,
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
            # The push is always folder-level and carries the new fields.
            assert event["stats"]["files_cloud_only"] == 0
            assert event["stats"]["dir"] is None
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


# ---------------------------------------------------------------------------
# rel_prefix scoping + cloud-only counting
# ---------------------------------------------------------------------------


def test_compute_scoped_to_rel_prefix(env: None) -> None:
    fid = _seed()
    _add_file(fid, "root.md", state="indexed", chunks=1, size=10)
    _add_file(fid, "sub/a.md", state="indexed", chunks=3, size=100, images=2)
    _add_file(fid, "sub/deep/b.pdf", state="pending", size=200)

    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder, rel_prefix="sub")
        full = compute_folder_stats(s, folder)

    assert out["dir"] == "sub"
    assert out["files_total"] == 2
    assert out["files_indexed"] == 1
    assert out["files_pending"] == 1
    assert out["chunks_total"] == 3
    assert out["bytes_total"] == 300
    assert out["images_total"] == 2
    assert set(out["by_extension"]) == {".md", ".pdf"}
    assert out["by_extension"][".md"]["files"] == 1
    # index_health is folder-level in BOTH modes (Qdrant points are
    # tracked per folder, not per subtree).
    assert out["index_health"] == full["index_health"]
    assert full["files_total"] == 3
    assert full["dir"] is None


def test_compute_scoped_prefix_respects_dir_boundary(env: None) -> None:
    fid = _seed()
    _add_file(fid, "sub/a.md", state="indexed")
    _add_file(fid, "subother/b.md", state="indexed")

    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder, rel_prefix="sub")

    assert out["files_total"] == 1  # 'subother/' must NOT match prefix 'sub'


def test_compute_scoped_unknown_prefix_returns_zeros(env: None) -> None:
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=2)

    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder, rel_prefix="nope")

    assert out["files_total"] == 0
    assert out["chunks_total"] == 0
    assert out["by_extension"] == {}


def test_compute_counts_cloud_only(env: None) -> None:
    fid = _seed()
    _add_file(
        fid, "rec.mp4", state="unsupported",
        error="cloud-only file — content not on disk",
    )
    # Unsupported for another reason and a genuine error must NOT count.
    _add_file(fid, "x.zzz", state="unsupported", error="no parser for .zzz")
    _add_file(fid, "y.pdf", state="error", error="cloud-only-ish but errored")

    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder)

    assert out["files_cloud_only"] == 1
    assert out["files_unsupported"] == 2  # cloud-only stays a subset


def test_compute_cloud_only_respects_scope(env: None) -> None:
    fid = _seed()
    _add_file(
        fid, "sub/rec.mp4", state="unsupported",
        error="cloud-only file — content not on disk",
    )
    _add_file(
        fid, "other/rec2.mp4", state="unsupported",
        error="cloud-only file — content not on disk",
    )

    with session_scope() as s:
        folder = s.get(Folder, fid)
        out = compute_folder_stats(s, folder, rel_prefix="sub")

    assert out["files_cloud_only"] == 1


# ---------------------------------------------------------------------------
# include_health (Tier-1 #3): live pushes carry SQLite-only stats
# ---------------------------------------------------------------------------


def test_include_health_toggle(env: None) -> None:
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=1)
    with session_scope() as s:
        folder = s.get(Folder, fid)
        full = compute_folder_stats(s, folder, include_health=True)
        lite = compute_folder_stats(s, folder, include_health=False)
    # REST/default path carries the health block…
    assert "index_health" in full
    assert set(full["index_health"]) == {"status", "qdrant_chunk_points"}
    # …the live-push path omits it (no per-publish Qdrant count).
    assert "index_health" not in lite
    # Everything else is identical.
    assert {k: v for k, v in full.items() if k != "index_health"} == lite


@pytest.mark.asyncio
async def test_publish_defaults_to_no_health(env: None) -> None:
    """publish_folder_stats now defaults include_health=False — the live WS
    push must not carry index_health (nor fire a Qdrant count)."""
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=1)
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["folders"]) as sub:
            with session_scope() as s:
                publish_folder_stats(s, fid)
            await sub.wait(timeout=1.0)
            (event,) = sub.drain()
            assert "index_health" not in event["stats"]
    finally:
        events.uninstall_loop()


# ---------------------------------------------------------------------------
# Cross-folder isolation — the JOIN aggregates must never count another
# folder's chunks/images (the blast radius the old IN(ids) approach and the
# new JOIN both must respect).
# ---------------------------------------------------------------------------


def test_cross_folder_isolation(env: None) -> None:
    a = _seed("/tmp/a", "a")
    b = _seed("/tmp/b", "b")
    _add_file(a, "a1.md", state="indexed", chunks=3, images=2)
    _add_file(b, "b1.md", state="indexed", chunks=5, images=1)
    _add_file(b, "b2.md", state="indexed", chunks=7, images=4)
    with session_scope() as s:
        sa = compute_folder_stats(s, s.get(Folder, a))
        sb = compute_folder_stats(s, s.get(Folder, b))
    assert sa["chunks_total"] == 3 and sa["images_total"] == 2 and sa["files_total"] == 1
    assert sb["chunks_total"] == 12 and sb["images_total"] == 5 and sb["files_total"] == 2


def test_rel_prefix_underscore_not_overmatched(env: None) -> None:
    """A subdir literally containing '_' must not LIKE-match arbitrary chars
    (regression guard for the escape in _file_filter)."""
    fid = _seed()
    _add_file(fid, "a_b/in.md", state="indexed", chunks=2)
    _add_file(fid, "aXb/out.md", state="indexed", chunks=9)  # would match 'a_b/%' unescaped
    with session_scope() as s:
        out = compute_folder_stats(s, s.get(Folder, fid), rel_prefix="a_b")
    assert out["files_total"] == 1 and out["chunks_total"] == 2


# ---------------------------------------------------------------------------
# Debounce (Tier-1 #1): mark-dirty fallback + flusher coalescing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_dirty_falls_back_to_sync_publish(env: None) -> None:
    """With no flusher running, mark_folder_stats_dirty publishes now."""
    assert fs_mod._flusher_running is False
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=1)
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["folders"]) as sub:
            mark_folder_stats_dirty(fid)  # no session arg — opens its own
            await sub.wait(timeout=1.0)
            (event,) = sub.drain()
            assert event["type"] == "folder.stats_changed"
            assert event["stats"]["chunks_total"] == 1
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_flusher_coalesces_burst_to_one_publish(env: None) -> None:
    """N marks for the same folder within a tick → exactly one publish."""
    fid = _seed()
    _add_file(fid, "a.md", state="indexed", chunks=1)
    events.install_loop(asyncio.get_running_loop())
    task = asyncio.create_task(run_stats_flusher(interval=0.1))
    try:
        # Let the flusher flip _flusher_running=True.
        await asyncio.sleep(0.02)
        assert fs_mod._flusher_running is True
        async with events.subscribe(["folders"]) as sub:
            for _ in range(20):
                mark_folder_stats_dirty(fid)  # coalesces into one dirty entry
            await sub.wait(timeout=1.0)
            await asyncio.sleep(0.15)  # allow the tick + any trailing
            events_out = sub.drain()
            stats_events = [e for e in events_out if e["type"] == "folder.stats_changed"]
            assert len(stats_events) == 1
            assert stats_events[0]["folder_id"] == fid
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        events.uninstall_loop()
        assert fs_mod._flusher_running is False
