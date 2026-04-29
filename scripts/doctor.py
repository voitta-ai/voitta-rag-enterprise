"""Print resolved settings and probe critical dependencies.

Usage::

    python -m scripts.doctor

Exits with code 1 if any required check fails. Skipped checks (model
downloads in fake-embedder mode) don't fail the run.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from voitta_image_rag.config import get_settings

logger = logging.getLogger(__name__)


def _ok(msg: str) -> None:
    print(f"  \033[32mok\033[0m   {msg}")


def _warn(msg: str) -> None:
    print(f"  \033[33mwarn\033[0m {msg}")


def _fail(msg: str) -> None:
    print(f"  \033[31mfail\033[0m {msg}")


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    failures = 0
    settings = get_settings()

    _section("Settings")
    print(f"  data_dir         : {settings.data_dir}")
    print(f"  db_path          : {settings.resolved_db_path()}")
    print(f"  cas_dir          : {settings.resolved_cas_dir()}")
    print(f"  qdrant_url       : {settings.qdrant_url or '(embedded)'}")
    print(f"  qdrant_path      : {settings.resolved_qdrant_path()}")
    print(f"  root_path        : {settings.root_path or '(unset — managed folders disabled)'}")
    print(f"  port             : {settings.port}")
    print(f"  mcp_port         : {settings.mcp_port}")
    print(f"  workers          : {settings.resolved_workers()}")
    print(f"  dense_model      : {settings.dense_model} ({settings.dense_version})")
    print(f"  sparse_model     : {settings.sparse_model} ({settings.sparse_version})")
    print(f"  image_model      : {settings.image_model} ({settings.image_version})")
    print(f"  use_fake_embed   : {settings.use_fake_embedders}")
    if settings.single_user:
        print("  auth             : single-user (root)")
    elif settings.dev_user:
        print(f"  auth             : dev-user ({settings.dev_user})")
    else:
        print("  auth             : multi-user (X-Forwarded-Email / X-User-Name)")

    _section("Filesystem")
    for label, path in [
        ("data_dir", settings.data_dir),
        ("cas_dir", settings.resolved_cas_dir()),
        ("qdrant_path", settings.resolved_qdrant_path()),
    ]:
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            test = Path(path) / ".doctor.tmp"
            test.write_text("x")
            test.unlink()
            _ok(f"{label} writable: {path}")
        except OSError as e:
            _fail(f"{label} not writable ({e}): {path}")
            failures += 1

    if settings.root_path is not None:
        try:
            Path(settings.root_path).mkdir(parents=True, exist_ok=True)
            test = Path(settings.root_path) / ".doctor.tmp"
            test.write_text("x")
            test.unlink()
            _ok(f"root_path writable: {settings.root_path}")
        except OSError as e:
            _fail(f"root_path not writable ({e}): {settings.root_path}")
            failures += 1

    _section("SQLite")
    try:
        from voitta_image_rag.db.database import init_db, session_scope
        from voitta_image_rag.db.models import Folder

        init_db()
        with session_scope() as s:
            n = s.query(Folder).count()
        _ok(f"database opens; {n} folder row(s)")
    except Exception as e:
        _fail(f"database error: {e}")
        failures += 1

    _section("Qdrant")
    try:
        from voitta_image_rag.services import vector_store

        def _probe():
            client = vector_store.get_client()
            return client.get_collections()

        result = vector_store.run_on_qdrant(_probe)
        names = [c.name for c in result.collections]
        _ok(f"reachable; collections: {names or '(none)'}")
    except Exception as e:
        _fail(f"qdrant error: {e}")
        failures += 1

    _section("Embedders")
    if settings.use_fake_embedders:
        _warn("VOITTA_USE_FAKE_EMBEDDERS=true — real models not loaded")
    else:
        for label, fn in [
            ("dense (e5)", _check_dense),
            ("sparse (BM25)", _check_sparse),
            ("image (CLIP/SigLIP)", _check_image),
        ]:
            try:
                fn()
                _ok(label)
            except ImportError as e:
                _warn(f"{label}: optional dep missing — install with `[ml]` ({e})")
            except Exception as e:
                _fail(f"{label}: {e}")
                failures += 1

    _section("Auth seed")
    if settings.single_user:
        _ok("single-user mode; users.txt skipped")
    elif settings.users_file.exists():
        with open(settings.users_file) as fh:
            users = [
                line.strip()
                for line in fh
                if line.strip() and not line.strip().startswith("#")
            ]
        _ok(f"users.txt found at {settings.users_file} ({len(users)} email(s))")
    else:
        _warn(f"users.txt not found at {settings.users_file} (multi-user mode)")

    print()
    if failures:
        print(f"FAIL ({failures} issue{'s' if failures > 1 else ''})")
        return 1
    print("OK")
    return 0


def _check_dense() -> None:
    from voitta_image_rag.services.embedding import get_text_embedder

    e = get_text_embedder()
    e.embed_query("warmup")


def _check_sparse() -> None:
    from voitta_image_rag.services.embedding import get_sparse_embedder

    e = get_sparse_embedder()
    e.embed_query("warmup")


def _check_image() -> None:
    from voitta_image_rag.services.embedding import get_image_embedder

    e = get_image_embedder()
    e.embed_text("warmup")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "WARNING"), format="%(levelname)s %(message)s"
    )
    sys.exit(main())
