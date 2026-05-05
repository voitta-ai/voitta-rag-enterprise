"""Smoke tests for ``scripts/`` CLIs."""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from pathlib import Path

from PIL import Image as PILImage
from sqlalchemy import select

from voitta_rag_enterprise.cas import store as cas_store
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Folder, Image, User
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


def _seed_full(folder_root: Path, layout: dict[str, str | bytes]) -> int:
    folder_root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = folder_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
    init_db()
    with session_scope() as s:
        folder = Folder(path=str(folder_root), display_name=folder_root.name)
        s.add(folder)
        s.flush()
        folder_id = folder.id
        for rel in layout:
            stat = (folder_root / rel).stat()
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


def test_seed_users_script(env: None, tmp_path: Path) -> None:
    init_db()
    p = tmp_path / "users.txt"
    p.write_text("alice@example.com\nbob@example.com\n# comment\n\n")

    from scripts import seed_users

    sys.argv = ["seed_users", str(p)]
    seed_users.main()

    with session_scope() as s:
        emails = sorted(u.email for u in s.execute(select(User)).scalars())
    assert emails == ["alice@example.com", "bob@example.com"]


def test_seed_users_missing_file(env: None, tmp_path: Path) -> None:
    from scripts import seed_users

    sys.argv = ["seed_users", str(tmp_path / "absent.txt")]
    try:
        seed_users.main()
    except SystemExit as e:
        assert e.code == 1
    else:
        raise AssertionError("expected SystemExit")


def test_rebuild_index_resets_state(env: None, tmp_path: Path) -> None:
    _seed_full(tmp_path / "src", {"a.md": "alpha beta gamma"})

    with session_scope() as s:
        before_files = list(s.execute(select(File)).scalars())
        assert all(f.state == "indexed" for f in before_files)
        assert s.query(Chunk).count() >= 1

    from scripts import rebuild_index

    sys.argv = ["rebuild_index", "--yes"]
    rebuild_index.main()

    with session_scope() as s:
        after_files = list(s.execute(select(File)).scalars())
        assert {f.id for f in after_files} == {f.id for f in before_files}
        assert all(f.state == "pending" for f in after_files)
        assert all(f.file_cas_id is None for f in after_files)
        assert s.query(Chunk).count() == 0
        assert s.query(Image).count() == 0
    # CAS dirs gone
    assert not cas_store.files_dir().exists()
    assert not cas_store.images_dir().exists()


def test_doctor_returns_zero_on_healthy_env(env: None, capsys) -> None:
    init_db()
    from scripts import doctor

    rc = doctor.main()
    captured = capsys.readouterr()
    assert "Settings" in captured.out
    assert "SQLite" in captured.out
    assert "Qdrant" in captured.out
    assert rc == 0


def test_reembed_stale_dry_run_reports(
    env: None, tmp_path: Path, monkeypatch, caplog
) -> None:
    """Bump dense_version in env; reembed_stale should detect file as stale."""
    import logging

    _seed_full(tmp_path / "src", {"a.md": "alpha beta gamma"})

    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_DENSE_VERSION", "e5-base-v2@2")  # bump
    reset_settings_cache()

    from scripts import reembed_stale

    sys.argv = ["reembed_stale", "--dry-run"]
    with caplog.at_level(logging.INFO, logger="scripts.reembed_stale"):
        reembed_stale.main()
    assert any("1 file(s)" in r.getMessage() for r in caplog.records)


def test_doctor_runs_as_subprocess(tmp_path: Path) -> None:
    """End-to-end: invoke the script as a Python subprocess."""
    env = {
        "PATH": "/usr/bin:/bin",
        "VOITTA_DATA_DIR": str(tmp_path / "data"),
        "VOITTA_DISABLE_BACKGROUND": "true",
        "VOITTA_USE_FAKE_EMBEDDERS": "true",
        "VOITTA_DEV_USER": "dev@localhost",
    }
    result = subprocess.run(
        [sys.executable, "-m", "scripts.doctor"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
