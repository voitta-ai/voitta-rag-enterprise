"""scripts/migrate_embedding_profile.py end to end (fake embedders)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import Chunk, CollectionMeta, Image
from voitta_rag_enterprise.services import vector_store as vs
from voitta_rag_enterprise.services.embedding import profiles, reset_embedder_caches

from .test_search_route import _png, _seed_and_index

FILES = {
    "notes/a.md": "alpha beta gamma delta " * 40,
    "notes/b.md": "epsilon zeta eta theta " * 40,
    "fig.png": _png(),
    "copy/fig.png": _png(),  # same bytes: one image point, two files
}


def _restart(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """What a process restart does to our caches."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    reset_embedder_caches()
    vs.reset_client_cache()


def _counts() -> dict[str, int]:
    return vs.collection_point_counts(list(profiles.ALL_COLLECTIONS))


def _file_chunk_points(collection: str) -> int:
    """Chunk points that belong to files (folder cards excluded: this test
    harness runs no background jobs, so the source never built cards)."""
    return sum(
        1
        for p in vs.scan_payload_fields(collection, ["kind"])
        if p.get("kind") != vs.FOLDER_CARD_KIND
    )


def _migrate(*args: str) -> int:
    from scripts import migrate_embedding_profile

    rc = migrate_embedding_profile.main(list(args))
    reset_embedder_caches()  # the script pinned its target profile
    return rc


@pytest.fixture
def legacy_install(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    """An install indexed under e5-siglip; returns the folder id."""
    from voitta_rag_enterprise.main import create_app

    _restart(monkeypatch, VOITTA_DEFAULT_EMBEDDING_PROFILE="e5-siglip", VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:
        folder_id = _seed_and_index(client, tmp_path / "docs", FILES)
    # The upgrade: the install default becomes gemma2, nothing else changes.
    monkeypatch.delenv("VOITTA_DEFAULT_EMBEDDING_PROFILE")
    _restart(monkeypatch)
    return folder_id


def test_dry_run_changes_nothing(legacy_install: int, capsys) -> None:
    before = _counts()
    assert _migrate("--to", "gemma2", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "chunks__gemma2" in out and "still to do" in out
    assert _counts() == before
    assert profiles.read_stored_profile() is None
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) is None


def test_migration_fills_the_target_and_switches_on_restart(
    legacy_install: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.main import create_app

    assert profiles.resolve().name == "e5-siglip"  # adopted before migrating
    src = _counts()
    assert _migrate("--to", "gemma2") == 0
    after = _counts()
    # Same file chunks and the same deduplicated images.
    assert _file_chunk_points("chunks__gemma2") == _file_chunk_points("chunks") == 2
    assert after["images__gemma2"] == src["images"] == 1
    # Source kept for rollback.
    assert after["chunks"] == src["chunks"] and after["images"] == src["images"]
    assert profiles.read_stored_profile() == "gemma2"
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) is None

    _restart(monkeypatch, VOITTA_DEV_USER="test@localhost")
    r = profiles.resolve()
    assert (r.name, r.source) == ("gemma2", "app_settings")
    prof = r.profile

    def payloads():
        c = vs.get_client()
        chunks, _ = c.scroll(prof.chunks_collection, limit=200, with_payload=True)
        images, _ = c.scroll(prof.images_collection, limit=10, with_payload=True)
        return chunks, images

    chunks, images = vs.run_on_qdrant(payloads)
    with session_scope() as s:
        sql_chunks = {c.id: c.file_id for c in s.query(Chunk)}
        image_files = {i.file_id for i in s.query(Image)}
    file_chunks = [p for p in chunks if p.payload.get("kind") != vs.FOLDER_CARD_KIND]
    assert {p.payload["chunk_id"]: p.payload["file_id"] for p in file_chunks} == sql_chunks
    assert {p.payload["dense_model_version"] for p in chunks} == {prof.dense_version}
    assert any(p.payload.get("kind") == vs.FOLDER_CARD_KIND for p in chunks)
    (img,) = images
    assert set(img.payload["file_ids"]) == image_files  # both copies attached
    assert img.payload["image_model_version"] == prof.image_version

    with TestClient(create_app()) as client:
        hits = client.post("/api/search", json={"query": "alpha beta", "modes": ["chunks", "images"]}).json()
        assert hits["chunks"] and hits["images"]


def test_rerun_is_a_no_op(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.services.indexing import embed

    assert _migrate("--to", "gemma2") == 0
    _restart(monkeypatch)
    calls = []
    real = embed.embed_file_chunks
    monkeypatch.setattr(embed, "embed_file_chunks", lambda fid, **kw: calls.append(fid) or real(fid, **kw))
    assert _migrate("--to", "gemma2") == 0
    assert calls == []
    # A catch-up run on a switched deployment never marks it in progress.
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) is None


def test_an_interrupted_migration_resumes_and_never_switches_early(
    legacy_install: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.services.indexing import embed

    real = embed.embed_file_chunks
    seen: list[int] = []

    def flaky(fid: int, **kw):
        if seen:
            raise KeyboardInterrupt  # operator hit ^C after the first file
        seen.append(fid)
        return real(fid, **kw)

    monkeypatch.setattr(embed, "embed_file_chunks", flaky)
    with pytest.raises(KeyboardInterrupt):
        _migrate("--to", "gemma2")
    monkeypatch.setattr(embed, "embed_file_chunks", real)

    # Restart mid-migration: still the old profile, never the half-filled one.
    _restart(monkeypatch)
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) == "gemma2"
    assert profiles.resolve().name == "e5-siglip"

    resumed: list[int] = []
    monkeypatch.setattr(embed, "embed_file_chunks", lambda fid, **kw: resumed.append(fid) or real(fid, **kw))
    _restart(monkeypatch)
    assert _migrate("--to", "gemma2") == 0
    assert seen[0] not in resumed  # already done, not redone
    assert profiles.read_stored_profile() == "gemma2"
    assert _file_chunk_points("chunks__gemma2") == _file_chunk_points("chunks")


def test_drop_old_only_after_a_completed_migration(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _migrate("--to", "gemma2", "--drop-old") == 2  # not migrated yet
    assert _counts()["chunks"] > 0
    assert _migrate("--to", "gemma2") == 0
    _restart(monkeypatch)
    assert _migrate("--to", "gemma2", "--drop-old") == 0
    counts = _counts()
    assert counts["chunks"] == counts["images"] == 0
    assert counts["chunks__gemma2"] > 0
    with session_scope() as s:
        assert {m.collection_name for m in s.query(CollectionMeta)} == {"chunks__gemma2", "images__gemma2"}
    _restart(monkeypatch)
    assert profiles.resolve().name == "gemma2"
