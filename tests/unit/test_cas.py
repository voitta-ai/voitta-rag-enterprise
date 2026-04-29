"""Tests for CAS store + refcounting."""

from __future__ import annotations

from pathlib import Path

from voitta_image_rag.cas import store
from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import CasRef


def test_hash_bytes_is_sha256() -> None:
    assert store.hash_bytes(b"hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_write_and_read_file_blob(env: None) -> None:
    sha = "0" * 64
    store.write_file_blob(sha, "text.md", "hello world")
    assert store.read_file_blob(sha, "text.md") == b"hello world"
    assert (store.file_dir(sha) / "text.md").exists()


def test_write_image_blob_returns_sha_and_dedup(env: None) -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    sha = store.write_image_blob(data)
    assert sha == store.hash_bytes(data)
    assert store.image_path(sha).exists()
    # Second write is a no-op but returns the same sha.
    sha2 = store.write_image_blob(data)
    assert sha2 == sha


def test_read_image_blob_round_trip(env: None) -> None:
    data = b"binary" * 50
    sha = store.write_image_blob(data)
    assert store.read_image_blob(sha) == data


def test_incref_creates_then_increments(env: None) -> None:
    init_db()
    with session_scope() as s:
        assert store.incref(s, store.KIND_FILE, "abc") == 1
        assert store.incref(s, store.KIND_FILE, "abc") == 2
        assert store.incref(s, store.KIND_FILE, "abc") == 3
    with session_scope() as s:
        ref = s.query(CasRef).one()
        assert ref.refcount == 3
        assert ref.last_decref_at is None


def test_decref_records_timestamp_when_zero(env: None) -> None:
    init_db()
    with session_scope() as s:
        store.incref(s, store.KIND_IMAGE, "img1")
        store.incref(s, store.KIND_IMAGE, "img1")
    with session_scope() as s:
        assert store.decref(s, store.KIND_IMAGE, "img1") == 1
    with session_scope() as s:
        ref = s.query(CasRef).one()
        assert ref.refcount == 1
        assert ref.last_decref_at is None  # not zero yet
    with session_scope() as s:
        assert store.decref(s, store.KIND_IMAGE, "img1") == 0
    with session_scope() as s:
        ref = s.query(CasRef).one()
        assert ref.refcount == 0
        assert ref.last_decref_at is not None


def test_incref_after_zero_clears_timestamp(env: None) -> None:
    init_db()
    with session_scope() as s:
        store.incref(s, store.KIND_FILE, "x")
        store.decref(s, store.KIND_FILE, "x")
    with session_scope() as s:
        ref = s.query(CasRef).one()
        assert ref.last_decref_at is not None
    with session_scope() as s:
        store.incref(s, store.KIND_FILE, "x")
    with session_scope() as s:
        ref = s.query(CasRef).one()
        assert ref.refcount == 1
        assert ref.last_decref_at is None


def test_decref_unknown_returns_zero(env: None) -> None:
    init_db()
    with session_scope() as s:
        assert store.decref(s, store.KIND_FILE, "missing") == 0


def test_remove_blob_file(env: None) -> None:
    sha = "0" * 64
    store.write_file_blob(sha, "text.md", "x")
    assert store.file_dir(sha).exists()
    assert store.remove_blob(store.KIND_FILE, sha) is True
    assert not store.file_dir(sha).exists()
    # Idempotent: removing a missing blob returns False.
    assert store.remove_blob(store.KIND_FILE, sha) is False


def test_remove_blob_image(env: None) -> None:
    sha = store.write_image_blob(b"data")
    assert store.image_path(sha).exists()
    assert store.remove_blob(store.KIND_IMAGE, sha) is True
    assert not store.image_path(sha).exists()


def test_cas_paths_under_data_dir(env: None) -> None:
    from voitta_image_rag.config import get_settings

    settings = get_settings()
    assert store.files_dir() == settings.data_dir / "cas" / "files"
    assert store.images_dir() == settings.data_dir / "cas" / "images"
    sha = "deadbeef" * 8
    assert store.file_dir(sha).parent == store.files_dir()
    assert store.image_path(sha).parent == store.images_dir()
    assert isinstance(store.image_path(sha), Path)
