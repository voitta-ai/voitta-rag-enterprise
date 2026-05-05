"""Unit tests for ``scan_folder`` and the sidecar loader."""

from __future__ import annotations

import json
from pathlib import Path

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder
from voitta_rag_enterprise.services.ignore import IgnoreMatcher
from voitta_rag_enterprise.services.scanner import load_sidecar, scan_folder


def _seed(root: Path, layout: dict[str, str]) -> None:
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _create_folder(session, path: Path) -> Folder:
    folder = Folder(path=str(path), display_name=path.name or str(path))
    session.add(folder)
    session.flush()
    return folder


def test_scan_inserts_new_files(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(src, {"a.txt": "a", "b/c.txt": "c"})

    with session_scope() as s:
        folder = _create_folder(s, src)
        result = scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)

    assert (result.added, result.updated, result.vanished) == (2, 0, 0)
    with session_scope() as s:
        rels = sorted(f.rel_path for f in s.query(File).all())
        assert rels == ["a.txt", "b/c.txt"]
        for f in s.query(File).all():
            assert f.state == "pending"
            assert f.size_bytes == 1


def test_scan_marks_vanished_files_deleted(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(src, {"keep.txt": "k", "gone.txt": "g"})

    with session_scope() as s:
        folder = _create_folder(s, src)
        scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)

    (src / "gone.txt").unlink()

    with session_scope() as s:
        folder = s.query(Folder).first()
        result = scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)

    assert result.vanished == 1
    with session_scope() as s:
        states = {f.rel_path: f.state for f in s.query(File).all()}
        assert states == {"keep.txt": "pending", "gone.txt": "deleted"}


def test_scan_revives_deleted_file(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(src, {"a.txt": "a"})
    with session_scope() as s:
        folder = _create_folder(s, src)
        scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)
    (src / "a.txt").unlink()
    with session_scope() as s:
        folder = s.query(Folder).first()
        scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)
    (src / "a.txt").write_text("a-again")
    with session_scope() as s:
        folder = s.query(Folder).first()
        result = scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)

    assert result.updated == 1
    with session_scope() as s:
        f = s.query(File).filter_by(rel_path="a.txt").one()
        assert f.state == "pending"


def test_scan_skips_oversize(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(src, {"small.txt": "x" * 50, "big.txt": "y" * 200})

    with session_scope() as s:
        folder = _create_folder(s, src)
        scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=100)

    with session_scope() as s:
        rels = [f.rel_path for f in s.query(File).all()]
        assert rels == ["small.txt"]


def test_scan_honours_ignore_matcher(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(
        src,
        {
            "keep.txt": "x",
            ".git/HEAD": "ref",
            "node_modules/lodash/index.js": "// js",
            "a/__pycache__/foo.pyc": "bytes",
        },
    )

    with session_scope() as s:
        folder = _create_folder(s, src)
        scan_folder(
            s,
            folder,
            IgnoreMatcher([".git", "node_modules", "__pycache__"]),
            max_file_bytes=10**9,
        )

    with session_scope() as s:
        rels = [f.rel_path for f in s.query(File).all()]
        assert rels == ["keep.txt"]


def test_scan_reads_sidecar_urls(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(src, {"a.md": "alpha", "b.py": "print(1)"})
    (src / ".voitta_sources.json").write_text(json.dumps({"a.md": "https://docs.example/a"}))

    with session_scope() as s:
        folder = _create_folder(s, src)
        scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)

    with session_scope() as s:
        urls = {f.rel_path: f.source_url for f in s.query(File).all()}
        assert urls == {"a.md": "https://docs.example/a", "b.py": None}


def test_scan_does_not_track_sidecar_itself(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(src, {"a.md": "alpha"})
    (src / ".voitta_sources.json").write_text("{}")

    with session_scope() as s:
        folder = _create_folder(s, src)
        scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)

    with session_scope() as s:
        rels = [f.rel_path for f in s.query(File).all()]
        assert rels == ["a.md"]


def test_load_sidecar_missing_returns_empty(tmp_path: Path) -> None:
    assert load_sidecar(tmp_path) == {}


def test_load_sidecar_invalid_json_returns_empty(tmp_path: Path) -> None:
    (tmp_path / ".voitta_sources.json").write_text("not json")
    assert load_sidecar(tmp_path) == {}


def test_load_sidecar_non_dict_returns_empty(tmp_path: Path) -> None:
    (tmp_path / ".voitta_sources.json").write_text('["a", "b"]')
    assert load_sidecar(tmp_path) == {}


def test_load_sidecar_accepts_object_form_with_tab(tmp_path: Path) -> None:
    (tmp_path / ".voitta_sources.json").write_text(
        json.dumps(
            {
                "Specs/01-Overview.md": {
                    "url": "https://docs.example/d/abc?tab=t.1",
                    "tab": "Overview",
                },
                "plain.pdf": "https://example/plain",
            }
        )
    )
    sidecar = load_sidecar(tmp_path)
    assert sidecar["Specs/01-Overview.md"].url == "https://docs.example/d/abc?tab=t.1"
    assert sidecar["Specs/01-Overview.md"].tab == "Overview"
    # Legacy string form still parses.
    assert sidecar["plain.pdf"].url == "https://example/plain"
    assert sidecar["plain.pdf"].tab is None


def test_scan_persists_tab_field_from_sidecar(env: None, tmp_path: Path) -> None:
    init_db()
    src = tmp_path / "src"
    _seed(src, {"Specs/01-Overview.md": "alpha"})
    (src / ".voitta_sources.json").write_text(
        json.dumps(
            {
                "Specs/01-Overview.md": {
                    "url": "https://docs.example/d/abc?tab=t.1",
                    "tab": "Overview",
                }
            }
        )
    )

    with session_scope() as s:
        folder = _create_folder(s, src)
        scan_folder(s, folder, IgnoreMatcher([]), max_file_bytes=10**9)

    with session_scope() as s:
        f = s.query(File).filter_by(rel_path="Specs/01-Overview.md").one()
        assert f.source_url == "https://docs.example/d/abc?tab=t.1"
        assert f.tab == "Overview"
