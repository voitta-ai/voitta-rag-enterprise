"""Tests for the CAS GC sweeper."""

from __future__ import annotations

import time

from voitta_rag_enterprise.cas import gc, store
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import CasRef


def test_sweep_skips_nonzero_refcount(env: None) -> None:
    init_db()
    sha = store.write_image_blob(b"x" * 10)
    with session_scope() as s:
        store.incref(s, store.KIND_IMAGE, sha)
    with session_scope() as s:
        result = gc.sweep(s, quiet_period_s=0)
    assert result.swept == 0
    assert store.image_path(sha).exists()


def test_sweep_skips_within_quiet_period(env: None) -> None:
    init_db()
    sha = store.write_image_blob(b"y" * 10)
    with session_scope() as s:
        store.incref(s, store.KIND_IMAGE, sha)
        store.decref(s, store.KIND_IMAGE, sha)
    with session_scope() as s:
        result = gc.sweep(s, quiet_period_s=60)
    assert result.swept == 0
    assert store.image_path(sha).exists()
    with session_scope() as s:
        assert s.query(CasRef).count() == 1


def test_sweep_removes_expired_image(env: None) -> None:
    init_db()
    sha = store.write_image_blob(b"z" * 10)
    with session_scope() as s:
        store.incref(s, store.KIND_IMAGE, sha)
        store.decref(s, store.KIND_IMAGE, sha)
    # Backdate last_decref_at past the quiet window.
    with session_scope() as s:
        ref = s.query(CasRef).one()
        ref.last_decref_at = int(time.time()) - 120

    with session_scope() as s:
        result = gc.sweep(s, quiet_period_s=60)
    assert result.swept == 1
    assert not store.image_path(sha).exists()
    with session_scope() as s:
        assert s.query(CasRef).count() == 0


def test_sweep_removes_expired_file_dir(env: None) -> None:
    init_db()
    sha = "f" * 64
    store.write_file_blob(sha, "text.md", "content")
    with session_scope() as s:
        store.incref(s, store.KIND_FILE, sha)
        store.decref(s, store.KIND_FILE, sha)
    with session_scope() as s:
        s.query(CasRef).one().last_decref_at = int(time.time()) - 120
    with session_scope() as s:
        result = gc.sweep(s, quiet_period_s=60)
    assert result.swept == 1
    assert not store.file_dir(sha).exists()


def test_sweep_skips_zero_refcount_with_no_timestamp(env: None) -> None:
    """A row at refcount=0 with last_decref_at=NULL is a fresh one — never decref'd."""
    init_db()
    with session_scope() as s:
        s.add(CasRef(cas_id="orphan", kind=store.KIND_IMAGE, refcount=0))
    with session_scope() as s:
        result = gc.sweep(s, quiet_period_s=0)
    assert result.swept == 0
