"""Unit tests for the NFS connector + path-safety helpers.

The connector is just os.walk + shutil.copy2, so the meat is in the
path-resolution logic that protects against ``..`` escapes and
symlink redirections outside the configured root. We exercise both
the helper function directly and the end-to-end ``list_children``
entry point.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services.sync import nfs as nfs_mod


# ---------------------------------------------------------------------------
# _resolve_under — pure-function path safety
# ---------------------------------------------------------------------------


def test_resolve_under_returns_root_for_empty_rel(tmp_path: Path) -> None:
    assert nfs_mod._resolve_under(tmp_path, "") == tmp_path.resolve()
    assert nfs_mod._resolve_under(tmp_path, "/") == tmp_path.resolve()


def test_resolve_under_descends_into_subdir(tmp_path: Path) -> None:
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert nfs_mod._resolve_under(tmp_path, "a/b") == sub.resolve()


def test_resolve_under_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="traversal"):
        nfs_mod._resolve_under(tmp_path, "../etc")
    with pytest.raises(ValueError, match="traversal"):
        nfs_mod._resolve_under(tmp_path, "a/../../etc")


def test_resolve_under_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        nfs_mod._resolve_under(tmp_path, "/etc/passwd")


def test_resolve_under_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink pointing outside the root is rejected after resolve."""
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "evil").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes the NFS root"):
        nfs_mod._resolve_under(root, "evil")


# ---------------------------------------------------------------------------
# list_children — admin gating + filesystem behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def nfs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temp dir as the admin NFS root via admin_store override."""
    settings_dir = tmp_path / "admin"
    settings_dir.mkdir()
    nfs_path = tmp_path / "share"
    nfs_path.mkdir()

    # Point admin_dir at our scratch admin dir, then save the nfs_root.
    monkeypatch.setattr(admin_store, "admin_dir", lambda: settings_dir)
    admin_store.save_settings({"nfs_root": str(nfs_path)})
    return nfs_path


def test_list_children_requires_configured_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_dir = tmp_path / "admin"
    settings_dir.mkdir()
    monkeypatch.setattr(admin_store, "admin_dir", lambda: settings_dir)
    # No nfs_root set → disabled.
    with pytest.raises(ValueError, match="not configured"):
        nfs_mod.list_children("")


def test_list_children_lists_subdirs_only(nfs_root: Path) -> None:
    (nfs_root / "alpha").mkdir()
    (nfs_root / "beta").mkdir()
    (nfs_root / "ignore.txt").write_text("hi")
    (nfs_root / ".hidden").mkdir()
    entries = nfs_mod.list_children("")
    names = [e["name"] for e in entries]
    assert names == ["alpha", "beta"]
    # rel_path is POSIX-style and rooted at the NFS root.
    assert entries[0]["rel_path"] == "alpha"


def test_list_children_descends(nfs_root: Path) -> None:
    (nfs_root / "alpha" / "sub").mkdir(parents=True)
    entries = nfs_mod.list_children("alpha")
    assert [e["name"] for e in entries] == ["sub"]
    assert entries[0]["rel_path"] == "alpha/sub"


def test_list_children_rejects_escape(nfs_root: Path) -> None:
    with pytest.raises(ValueError, match="traversal"):
        nfs_mod.list_children("../etc")


def test_list_children_404s_on_missing(nfs_root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        nfs_mod.list_children("nope")


def test_list_children_400s_when_path_is_file(nfs_root: Path) -> None:
    (nfs_root / "file.txt").write_text("hi")
    with pytest.raises(NotADirectoryError):
        nfs_mod.list_children("file.txt")


# ---------------------------------------------------------------------------
# NfsConnector.sync — end-to-end copy semantics
# ---------------------------------------------------------------------------


def test_connector_copies_files_into_folder_root(
    nfs_root: Path, tmp_path: Path
) -> None:
    # Build a tree under the NFS share.
    src_sub = nfs_root / "project"
    (src_sub / "docs").mkdir(parents=True)
    (src_sub / "docs" / "spec.md").write_text("# spec\n")
    (src_sub / "README.md").write_text("# readme\n")
    (src_sub / ".hidden").write_text("x")  # skipped

    folder_root = tmp_path / "folder"
    folder_root.mkdir()

    import asyncio

    stats = asyncio.run(
        nfs_mod.NfsConnector().sync(
            folder_root=folder_root,
            nfs_subpaths=["project"],
            progress_cb=None,
        )
    )
    assert stats.files_copied == 2  # spec.md + README.md
    # Multi-subpath connector preserves the FULL relative path under
    # the NFS root — so "project/" is part of the on-disk layout. This
    # keeps two-subdirectory selections from colliding.
    assert (folder_root / "project" / "docs" / "spec.md").read_text() == "# spec\n"
    assert (folder_root / "project" / "README.md").read_text() == "# readme\n"
    # Hidden files were skipped.
    assert not (folder_root / "project" / ".hidden").exists()
    sidecar = json.loads((folder_root / nfs_mod.SOURCES_SIDECAR).read_text())
    assert set(sidecar.keys()) == {"project/docs/spec.md", "project/README.md"}


def test_connector_multi_subpath_disjoint_trees(
    nfs_root: Path, tmp_path: Path
) -> None:
    """Two disjoint selections mirror into distinct namespaces.

    Both ``data/projectA`` and ``data/projectB`` should land at their
    full relative paths under the folder root with no collision.
    """
    (nfs_root / "data" / "projectA").mkdir(parents=True)
    (nfs_root / "data" / "projectB").mkdir(parents=True)
    (nfs_root / "data" / "projectA" / "a.txt").write_text("A")
    (nfs_root / "data" / "projectB" / "a.txt").write_text("B")

    folder_root = tmp_path / "folder"
    folder_root.mkdir()
    import asyncio
    stats = asyncio.run(
        nfs_mod.NfsConnector().sync(
            folder_root=folder_root,
            nfs_subpaths=["data/projectA", "data/projectB"],
            progress_cb=None,
        )
    )
    assert stats.files_copied == 2
    assert (folder_root / "data" / "projectA" / "a.txt").read_text() == "A"
    assert (folder_root / "data" / "projectB" / "a.txt").read_text() == "B"


def test_connector_skips_unchanged_files(
    nfs_root: Path, tmp_path: Path
) -> None:
    """A second sync without source changes copies nothing."""
    src_sub = nfs_root / "project"
    src_sub.mkdir()
    (src_sub / "a.txt").write_text("hello")

    folder_root = tmp_path / "folder"
    folder_root.mkdir()

    import asyncio

    asyncio.run(
        nfs_mod.NfsConnector().sync(
            folder_root=folder_root, nfs_subpaths=["project"], progress_cb=None
        )
    )
    stats2 = asyncio.run(
        nfs_mod.NfsConnector().sync(
            folder_root=folder_root, nfs_subpaths=["project"], progress_cb=None
        )
    )
    assert stats2.files_copied == 0
    assert stats2.files_unchanged == 1


def test_connector_removes_files_gone_from_source(
    nfs_root: Path, tmp_path: Path
) -> None:
    """Files claimed by the previous sidecar but missing from source
    are deleted from the folder root. Hand-uploaded files (not in
    the prior sidecar) survive."""
    src_sub = nfs_root / "project"
    src_sub.mkdir()
    (src_sub / "kept.txt").write_text("k")
    (src_sub / "doomed.txt").write_text("d")

    folder_root = tmp_path / "folder"
    folder_root.mkdir()
    (folder_root / "manual_upload.md").write_text("user added this")

    import asyncio

    asyncio.run(
        nfs_mod.NfsConnector().sync(
            folder_root=folder_root, nfs_subpaths=["project"], progress_cb=None
        )
    )
    # Manual upload was never claimed by NFS sidecar → still there.
    assert (folder_root / "manual_upload.md").exists()

    # Remove doomed.txt from source and re-sync.
    (src_sub / "doomed.txt").unlink()
    stats = asyncio.run(
        nfs_mod.NfsConnector().sync(
            folder_root=folder_root, nfs_subpaths=["project"], progress_cb=None
        )
    )
    assert stats.files_removed == 1
    assert not (folder_root / "project" / "doomed.txt").exists()
    # Other files unaffected.
    assert (folder_root / "project" / "kept.txt").read_text() == "k"
    assert (folder_root / "manual_upload.md").exists()


def test_connector_rejects_unconfigured_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_dir = tmp_path / "admin"
    settings_dir.mkdir()
    monkeypatch.setattr(admin_store, "admin_dir", lambda: settings_dir)
    folder_root = tmp_path / "folder"
    folder_root.mkdir()
    import asyncio

    with pytest.raises(RuntimeError, match="not configured"):
        asyncio.run(
            nfs_mod.NfsConnector().sync(
                folder_root=folder_root, nfs_subpaths=[""], progress_cb=None
            )
        )


def test_connector_rejects_empty_selection(
    nfs_root: Path, tmp_path: Path
) -> None:
    """No selection = error, even if the root is otherwise valid."""
    folder_root = tmp_path / "folder"
    folder_root.mkdir()
    import asyncio
    with pytest.raises(RuntimeError, match="pick at least one"):
        asyncio.run(
            nfs_mod.NfsConnector().sync(
                folder_root=folder_root, nfs_subpaths=[], progress_cb=None
            )
        )


# ---------------------------------------------------------------------------
# canonicalise_subpaths — selection normalisation
# ---------------------------------------------------------------------------


def test_canonicalise_dedups_and_drops_descendants() -> None:
    # ``a/b/c`` is redundant once ``a/b`` is selected — drop it.
    assert nfs_mod.canonicalise_subpaths(["a/b/c", "a/b", "x"]) == ["a/b", "x"]


def test_canonicalise_root_absorbs_everything() -> None:
    # Picking "" (the root) makes any other selection redundant.
    assert nfs_mod.canonicalise_subpaths(["", "a", "b/c"]) == [""]


def test_canonicalise_rejects_traversal_and_absolute() -> None:
    # ``..`` segments rejected; absolute paths likewise. The cleaner
    # also collapses repeated slashes ("//") and leading "./".
    assert nfs_mod.canonicalise_subpaths(["../etc", "a/../b", "ok"]) == ["ok"]


def test_canonicalise_strips_redundant_slashes() -> None:
    assert nfs_mod.canonicalise_subpaths(["a//b", "./a/b", "a/b/"]) == ["a/b"]
