"""Unit tests for the linked-folder pieces that need no HTTP layer.

* ``services.in_place`` — the one predicate every in-place rule keys on.
* ``services.ignore`` — per-folder patterns, normalisation, ``for_folder``.
* ``services.sync.localfs`` — the shared path-safety helpers (also exercised
  through the NFS wrapper in test_sync_nfs.py).
* ``LocalLinkConnector`` — sync() validates and copies NOTHING.
* scanner / watcher / startup-recovery behaviour for an in-place folder.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder, FolderSyncSource
from voitta_rag_enterprise.services import ignore as ignore_mod
from voitta_rag_enterprise.services.ignore import IgnoreMatcher
from voitta_rag_enterprise.services.in_place import (
    IN_PLACE_SOURCE_TYPES,
    indexes_in_place,
    is_in_place_type,
)
from voitta_rag_enterprise.services.scanner import scan_folder
from voitta_rag_enterprise.services.sync import localfs
from voitta_rag_enterprise.services.sync.local_link import LocalLinkConnector


# ---------------------------------------------------------------------------
# in_place predicate
# ---------------------------------------------------------------------------


def test_in_place_types_are_exactly_the_two_read_only_kinds() -> None:
    assert IN_PLACE_SOURCE_TYPES == {"google_drive_local", "local_link"}
    assert is_in_place_type("local_link")
    assert is_in_place_type("google_drive_local")
    for t in ("filesystem", "nfs", "github", "google_drive", None, ""):
        assert not is_in_place_type(t)


def test_indexes_in_place_reads_folder_source_type() -> None:
    assert indexes_in_place(Folder(path="/x", display_name="x", source_type="local_link"))
    assert not indexes_in_place(Folder(path="/x", display_name="x", source_type="nfs"))


# ---------------------------------------------------------------------------
# ignore: parse / normalize / for_folder
# ---------------------------------------------------------------------------


def test_parse_patterns_is_tolerant() -> None:
    assert ignore_mod.parse_patterns(None) == []
    assert ignore_mod.parse_patterns("") == []
    assert ignore_mod.parse_patterns("not json") == []
    assert ignore_mod.parse_patterns('{"a": 1}') == []
    assert ignore_mod.parse_patterns('["_blobs", 3, "", " ", "*.avif"]') == ["_blobs", "*.avif"]


def test_normalize_patterns_strips_dedups_and_keeps_order() -> None:
    assert ignore_mod.normalize_patterns([" _blobs ", "", "raw.html", "_blobs", "*.avif"]) == [
        "_blobs", "raw.html", "*.avif",
    ]


@pytest.mark.parametrize("bad", ["a/b", "sub\\dir", "/abs"])
def test_normalize_patterns_rejects_path_separators(bad: str) -> None:
    """The matcher tests path-component names; a pattern with a separator
    could never match and would be a silent no-op."""
    with pytest.raises(ValueError, match="path separator"):
        ignore_mod.normalize_patterns([bad])


def test_for_folder_combines_global_and_row_patterns(env: None, tmp_path: Path) -> None:
    init_db()
    with session_scope() as s:
        folder = Folder(path=str(tmp_path), display_name="t", source_type="local_link")
        s.add(folder)
        s.flush()
        s.add(FolderSyncSource(
            folder_id=folder.id, source_type="local_link",
            ll_path=str(tmp_path), ll_ignore=json.dumps(["_blobs", "raw.html"]),
        ))
        s.flush()
        m = ignore_mod.for_folder(s, folder)

    # Row patterns apply at any depth …
    assert m.matches("_blobs/ab/abcd/raw.pdf")
    assert m.matches("labs/x/2026/09/13/raw.html")
    # … global defaults still apply …
    assert m.matches("sub/.voitta_sources.json")
    # … and real content is untouched.
    assert not m.matches("labs/x/2026/09/13/content.md")
    assert not m.matches("labs/x/2026/09/13/raw.pdf")


def test_for_folder_without_a_row_is_just_the_global_list(env: None, tmp_path: Path) -> None:
    init_db()
    with session_scope() as s:
        folder = Folder(path=str(tmp_path), display_name="t")
        s.add(folder)
        s.flush()
        m = ignore_mod.for_folder(s, folder)
    assert m.matches(".voitta_sources.json")
    assert not m.matches("_blobs/x")


# ---------------------------------------------------------------------------
# localfs helpers
# ---------------------------------------------------------------------------


def test_resolve_under_labels_escape_errors(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-ll"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "evil").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes the linked-folder root"):
        localfs.resolve_under(root, "evil", root_label="linked-folder root")


def test_list_children_posix_rel_paths(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "file.txt").write_text("x")
    (tmp_path / "a" / ".hidden").mkdir()
    out = localfs.list_children(tmp_path, "a")
    assert out == [{"name": "b", "rel_path": "a/b"}]


def test_list_children_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="linked-folder root does not exist"):
        localfs.list_children(tmp_path / "nope", "", root_label="linked-folder root")


# ---------------------------------------------------------------------------
# LocalLinkConnector — validates, copies nothing
# ---------------------------------------------------------------------------


def test_connector_sync_copies_nothing_and_reports_path(tmp_path: Path) -> None:
    linked = tmp_path / "lake"
    (linked / "a").mkdir(parents=True)
    (linked / "a" / "x.md").write_text("hello")
    before = sorted(p.relative_to(linked) for p in linked.rglob("*"))

    stats = asyncio.run(
        LocalLinkConnector().sync(folder_root=linked, ignore=["_blobs"])
    )

    after = sorted(p.relative_to(linked) for p in linked.rglob("*"))
    assert after == before  # nothing written into the tree
    assert stats.as_dict() == {
        "path": str(linked), "indexed_in_place": True, "ignore": ["_blobs"], "errors": [],
    }


def test_connector_sync_rejects_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing or not a directory"):
        asyncio.run(LocalLinkConnector().sync(folder_root=tmp_path / "gone"))


def test_connector_resolve_config_reads_only_the_row() -> None:
    from types import SimpleNamespace

    row = SimpleNamespace(ll_ignore=json.dumps(["_blobs", "*.avif"]))
    assert LocalLinkConnector().resolve_config(row) == {"ignore": ["_blobs", "*.avif"]}
    assert LocalLinkConnector().resolve_config(SimpleNamespace(ll_ignore=None)) == {"ignore": []}


# ---------------------------------------------------------------------------
# Scanner on an in-place folder
# ---------------------------------------------------------------------------


def _lake(tmp_path: Path) -> Path:
    """A miniature ai-news lake: real content, a blob dir, bookkeeping, a raw page."""
    root = tmp_path / "lake"
    item = root / "labs" / "acme" / "2026" / "09" / "13" / "20" / "post-abc12345"
    item.mkdir(parents=True)
    (item / "content.md").write_text("# Post\n\nbody")
    (item / "raw.html").write_text("<html>nav ads body</html>")
    (item / "raw.pdf").write_bytes(b"%PDF-1.4")
    (item / "item.json").write_text("{}")
    (root / "_blobs" / "ab" / "abcd").mkdir(parents=True)
    (root / "_blobs" / "ab" / "abcd" / "raw.html").write_text("<html>dup</html>")
    (root / "_state").mkdir()
    (root / "_state" / "index.sqlite").write_bytes(b"\x00")
    return root


def test_scan_applies_row_ignore_and_writes_no_sidecar_into_the_tree(
    env: None, tmp_path: Path
) -> None:
    init_db()
    root = _lake(tmp_path)
    snapshot = sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    with session_scope() as s:
        folder = Folder(path=str(root), display_name="lake", source_type="local_link")
        s.add(folder)
        s.flush()
        s.add(FolderSyncSource(
            folder_id=folder.id, source_type="local_link", ll_path=str(root),
            ll_ignore=json.dumps(["_blobs", "_state", "raw.html"]),
        ))
        s.flush()
        result = scan_folder(s, folder, max_file_bytes=10**9)  # ignore=None → for_folder

    assert result.added == 3
    with session_scope() as s:
        rels = sorted(f.rel_path for f in s.query(File).all())
    assert rels == [
        "labs/acme/2026/09/13/20/post-abc12345/content.md",
        "labs/acme/2026/09/13/20/post-abc12345/item.json",
        "labs/acme/2026/09/13/20/post-abc12345/raw.pdf",
    ]
    # The tree is byte-for-byte what it was: no sidecar, no copies.
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == snapshot
    assert not (root / ".voitta_sources.json").exists()


def test_scan_with_explicit_matcher_still_works(env: None, tmp_path: Path) -> None:
    """The ``ignore`` parameter keeps its old meaning for callers that pass one."""
    init_db()
    root = _lake(tmp_path)
    with session_scope() as s:
        folder = Folder(path=str(root), display_name="lake")
        s.add(folder)
        s.flush()
        result = scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)
    assert result.added == 6  # everything, including _blobs and _state


# ---------------------------------------------------------------------------
# Watcher never attaches to an in-place folder
# ---------------------------------------------------------------------------


def test_watcher_skips_in_place_folders(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.services.watcher import WatcherManager

    init_db()
    root = tmp_path / "lake"
    root.mkdir()
    with session_scope() as s:
        linked = Folder(path=str(root), display_name="lake", source_type="local_link")
        s.add(linked)
        s.flush()
        mgr = WatcherManager(debounce_s=0.1)
        mgr.watch(linked, max_file_bytes=10**9, ignore=IgnoreMatcher([]))
        assert linked.id not in mgr._watches
        assert linked.id not in mgr._managed_dirname

        # A regular folder is still watched. Under VOITTA_ROOT_PATH (which
        # conftest sets to tmp_path) that means the shared root watch, keyed
        # in _managed_dirname; the point is that it is registered at all.
        plain_root = tmp_path / "plain"
        plain_root.mkdir()
        plain = Folder(path=str(plain_root), display_name="plain")
        s.add(plain)
        s.flush()
        mgr.watch(plain, max_file_bytes=10**9, ignore=IgnoreMatcher([]))
        assert plain.id in mgr._managed_dirname or plain.id in mgr._watches
        mgr.stop()


# ---------------------------------------------------------------------------
# Startup recovery honours the folder's own ignore list
# ---------------------------------------------------------------------------


def test_recovery_treats_row_ignored_file_as_absent(env: None, tmp_path: Path) -> None:
    """A row for a file the folder now ignores must not be resurrected —
    recovery and the scan must agree on what counts."""
    from voitta_rag_enterprise.services.startup_recovery import run_startup_recovery

    init_db()
    root = _lake(tmp_path)
    with session_scope() as s:
        folder = Folder(path=str(root), display_name="lake", source_type="local_link")
        s.add(folder)
        s.flush()
        s.add(FolderSyncSource(
            folder_id=folder.id, source_type="local_link", ll_path=str(root),
            ll_ignore=json.dumps(["raw.html"]),
        ))
        s.flush()
        # A stale 'deleted' row for a now-ignored file that IS on disk.
        f = File(
            folder_id=folder.id,
            rel_path="labs/acme/2026/09/13/20/post-abc12345/raw.html",
            state="deleted",
        )
        s.add(f)
        s.flush()
        fid = f.id

    report = run_startup_recovery()

    assert report.resurrected_files == 0
    with session_scope() as s:
        assert s.get(File, fid).state == "deleted"
