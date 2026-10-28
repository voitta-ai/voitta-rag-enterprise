"""scripts/migrate_embedding_profile.py end to end (fake embedders, real SQLite + Qdrant)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.cas import store as cas_store
from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import Chunk, CollectionMeta, File, Image
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


def _migrate(*args: str) -> int:
    from scripts import migrate_embedding_profile

    rc = migrate_embedding_profile.main(list(args))
    reset_embedder_caches()  # the script pinned its target profile
    return rc


def _points(collection: str) -> list:
    def _do():
        pts, _ = vs.get_client().scroll(collection, limit=500, with_payload=True)
        return pts

    return vs.run_on_qdrant(_do)


@pytest.fixture
def legacy_install(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    """An install that started on e5-siglip (recorded) and was then upgraded
    to a build whose default is gemma2. Returns the folder id."""
    from voitta_rag_enterprise.main import create_app

    _restart(monkeypatch, VOITTA_DEFAULT_EMBEDDING_PROFILE="e5-siglip", VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:
        folder_id = _seed_and_index(client, tmp_path / "docs", FILES)
    monkeypatch.delenv("VOITTA_DEFAULT_EMBEDDING_PROFILE")
    _restart(monkeypatch)
    assert profiles.read_stored_profile() == "e5-siglip"
    return folder_id


def _file_chunks(collection: str) -> int:
    """Chunk points excluding folder cards (the fixture's indexing never
    builds cards for the source; the migration builds them for the target)."""
    return sum(1 for p in _points(collection) if p.payload.get("kind") != vs.FOLDER_CARD_KIND)


def _bulk_and_finish() -> None:
    assert _migrate("--to", "gemma2") == 0
    assert _migrate("--to", "gemma2", "--finish") == 0


def test_dry_run_changes_nothing(legacy_install: int, capsys) -> None:
    before = _counts()
    assert _migrate("--to", "gemma2", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "chunks__gemma2" in out and "still to do" in out and "served now     : e5-siglip" in out
    assert _counts() == before
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) is None


def test_bulk_fills_the_target_without_switching(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _counts()
    assert _migrate("--to", "gemma2") == 0
    after = _counts()
    assert _file_chunks("chunks__gemma2") == _file_chunks("chunks")
    assert after["images__gemma2"] == src["images"] == 1
    # The served profile's collections are untouched, and still served.
    assert (after["chunks"], after["images"]) == (src["chunks"], src["images"])
    assert profiles.read_stored_profile() == "e5-siglip"
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) == "gemma2"
    _restart(monkeypatch)
    assert profiles.resolve().name == "e5-siglip"


def test_finish_switches_and_the_app_serves_the_target(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.main import create_app

    _bulk_and_finish()
    assert profiles.read_stored_profile() == "gemma2"
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) is None

    _restart(monkeypatch, VOITTA_DEV_USER="test@localhost")
    r = profiles.resolve()
    assert (r.name, r.source) == ("gemma2", "app_settings")
    prof = r.profile
    chunks, images = _points(prof.chunks_collection), _points(prof.images_collection)
    with session_scope() as s:
        sql_chunks = {c.id: (c.file_id, c.chunk_hash) for c in s.query(Chunk)}
        image_files = {i.file_id for i in s.query(Image).where(Image.kind == "figure")}
    file_chunks = [p for p in chunks if p.payload.get("kind") != vs.FOLDER_CARD_KIND]
    assert {p.payload["chunk_id"]: (p.payload["file_id"], p.payload["chunk_hash"]) for p in file_chunks} == sql_chunks
    assert {p.payload["dense_model_version"] for p in chunks} == {prof.dense_version}
    assert any(p.payload.get("kind") == vs.FOLDER_CARD_KIND for p in chunks)
    (img,) = images
    assert set(img.payload["file_ids"]) == image_files  # both copies attached
    assert img.payload["image_model_version"] == prof.image_version

    with TestClient(create_app()) as client:
        hits = client.post("/api/search", json={"query": "alpha beta", "modes": ["chunks", "images"]}).json()
        assert hits["chunks"] and hits["images"]


def test_finish_picks_up_what_the_app_indexed_during_the_bulk_phase(
    legacy_install: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from voitta_rag_enterprise.main import create_app

    assert _migrate("--to", "gemma2") == 0
    # The app keeps serving e5-siglip and indexes a new folder meanwhile.
    _restart(monkeypatch, VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:
        _seed_and_index(client, tmp_path / "later", {"late.md": "omicron pi rho sigma " * 30})
    assert _migrate("--to", "gemma2", "--finish") == 0
    with session_scope() as s:
        late = s.execute(select(File).where(File.rel_path == "late.md")).scalar_one().id
    assert any(p.payload.get("file_id") == late for p in _points("chunks__gemma2"))


def test_rerunning_a_finished_migration_is_a_no_op(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.services.indexing import embed

    _bulk_and_finish()
    _restart(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(embed, "embed_file_chunks", lambda fid, **kw: calls.append(fid))
    assert _migrate("--to", "gemma2") == 0
    assert _migrate("--to", "gemma2", "--finish") == 0
    assert calls == []
    assert profiles.read_setting(profiles.MIGRATION_SETTING_KEY) is None


def test_an_interrupted_bulk_phase_resumes(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.services.indexing import embed

    real = embed.embed_file_chunks
    done: list[int] = []

    def interrupted(fid: int, **kw):
        if done:
            raise KeyboardInterrupt  # operator hit ^C after the first file
        done.append(fid)
        return real(fid, **kw)

    monkeypatch.setattr(embed, "embed_file_chunks", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _migrate("--to", "gemma2")
    resumed: list[int] = []
    monkeypatch.setattr(embed, "embed_file_chunks", lambda fid, **kw: resumed.append(fid) or real(fid, **kw))
    _restart(monkeypatch)
    assert profiles.resolve().name == "e5-siglip"  # restart mid-migration: old profile
    assert _migrate("--to", "gemma2", "--finish") == 0
    assert done[0] not in resumed
    assert _file_chunks("chunks__gemma2") == _file_chunks("chunks")


def test_page_renders_are_not_migrated(legacy_install: int) -> None:
    """Review #3: the indexer never embeds page renders; neither may the migration."""
    with session_scope() as s:
        fid = s.execute(select(File.id).where(File.rel_path == "notes/a.md")).scalar_one()
        cas = cas_store.write_image_blob(b"\x89PNG-render" + bytes(64))
        s.add(Image(file_id=fid, image_index=99, image_cas_id=cas, kind="page_render", page=1))
    _bulk_and_finish()
    assert {p.payload["image_cas_id"] for p in _points("images__gemma2")} == {
        p.payload["image_cas_id"] for p in _points("images")
    }


def test_content_change_with_reused_chunk_ids_is_re_embedded(
    legacy_install: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #5: SQLite reuses chunk ids when a file is re-chunked, so the
    diff must compare content hashes too."""
    from voitta_rag_enterprise.services.indexing import embed

    assert _migrate("--to", "gemma2") == 0
    with session_scope() as s:
        fid = s.execute(select(File.id).where(File.rel_path == "notes/b.md")).scalar_one()
        for c in s.execute(select(Chunk).where(Chunk.file_id == fid)).scalars():
            c.text, c.chunk_hash = "rewritten " + c.text, "0" * 64
    calls: list[int] = []
    real = embed.embed_file_chunks
    monkeypatch.setattr(embed, "embed_file_chunks", lambda f, **kw: calls.append(f) or real(f, **kw))
    assert _migrate("--to", "gemma2", "--finish") == 0
    assert calls == [fid]
    texts = {p.payload["text"] for p in _points("chunks__gemma2") if p.payload.get("file_id") == fid}
    assert all(t.startswith("rewritten ") for t in texts)


def test_a_shared_image_survives_its_owner_being_deleted(legacy_install: int) -> None:
    """Review #6: the image point is keyed by its first row; deleting that
    file must re-point the point to the other copy, not drop it."""
    _bulk_and_finish()
    (point,) = _points("images__gemma2")
    owner = point.payload["image_id"]
    with session_scope() as s:
        owner_file = s.get(Image, owner).file_id
        s.execute(Image.__table__.delete().where(Image.id == owner))
        (other,) = s.execute(select(Image.id).where(Image.kind == "figure")).scalars()
    with session_scope() as s:
        live = dict(s.execute(select(Image.id, Image.image_cas_id).where(Image.kind == "figure")).all())
    profiles.pin("gemma2")
    assert vs.delete_orphan_image_points(live) == 0
    (point,) = _points("images__gemma2")
    # Re-keyed: the point id is the live row's, never a dead (reusable) one.
    assert point.id == point.payload["image_id"] == other != owner
    assert owner_file in point.payload["file_ids"]  # file detachment is the deleter's job
    # With no figure row left at all the sweep refuses rather than empty
    # the collection (that is a wrong database, not a stale point).
    with pytest.raises(vs.OrphanSweepRefused):
        vs.delete_orphan_image_points({})
    assert len(_points("images__gemma2")) == 1


def test_a_failing_item_is_reported_and_blocks_the_switch(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review #7: one missing blob must not abort the run or loop forever."""
    with session_scope() as s:
        cas = s.execute(select(Image.image_cas_id).where(Image.kind == "figure")).scalars().first()
    cas_store.image_path(cas).unlink()
    assert _migrate("--to", "gemma2") == 0  # bulk completes despite it
    assert _file_chunks("chunks__gemma2") == _file_chunks("chunks")
    assert _migrate("--to", "gemma2", "--finish") == 1
    assert profiles.read_stored_profile() == "e5-siglip"
    assert _migrate("--to", "gemma2", "--finish", "--accept-failures") == 0
    assert profiles.read_stored_profile() == "gemma2"


def test_stale_image_versions_are_re_embedded_in_place(legacy_install: int) -> None:
    """Review #10: an image point with an old version stamp is re-embedded
    into the same point (no duplicate), and the migration converges."""
    assert _migrate("--to", "gemma2") == 0
    (point,) = _points("images__gemma2")

    def _stamp_old():
        vs.get_client().set_payload("images__gemma2", payload={"image_model_version": "old@0"}, points=[point.id])

    vs.run_on_qdrant(_stamp_old)
    assert _migrate("--to", "gemma2", "--finish") == 0
    (after,) = _points("images__gemma2")
    assert after.id == point.id
    assert after.payload["image_model_version"] == profiles.profile("gemma2").image_version
    assert set(after.payload["file_ids"]) == set(point.payload["file_ids"])


def test_drop_old_only_once_the_target_is_served(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review #9."""
    assert _migrate("--to", "gemma2", "--drop-old") == 2  # nothing migrated
    assert _migrate("--to", "gemma2") == 0
    assert _migrate("--to", "gemma2", "--drop-old") == 2  # bulk only, not finished
    assert _counts()["chunks"] > 0
    assert _migrate("--to", "gemma2", "--finish") == 0
    _restart(monkeypatch)
    assert _migrate("--to", "gemma2", "--drop-old") == 0
    counts = _counts()
    assert counts["chunks"] == counts["images"] == 0
    assert counts["chunks__gemma2"] > 0
    with session_scope() as s:
        assert {m.collection_name for m in s.query(CollectionMeta)} == {"chunks__gemma2", "images__gemma2"}
    _restart(monkeypatch)
    assert profiles.resolve().name == "gemma2"


def test_drop_old_refuses_an_empty_target(legacy_install: int, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles.write_settings({profiles.PROFILE_SETTING_KEY: "gemma2"})
    assert _migrate("--to", "gemma2", "--drop-old") == 2
    assert _counts()["chunks"] > 0


def test_rollback_is_a_migration_back(legacy_install: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    _bulk_and_finish()
    _restart(monkeypatch, VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:  # now on gemma2; index something new
        _seed_and_index(client, tmp_path / "new", {"n.md": "upsilon phi chi psi " * 30})
    _restart(monkeypatch, VOITTA_EMBEDDING_PROFILE="e5-siglip")
    with pytest.raises(profiles.ProfileMismatchError):
        profiles.resolve()  # flipping the env is refused
    monkeypatch.delenv("VOITTA_EMBEDDING_PROFILE")
    _restart(monkeypatch)
    assert _migrate("--to", "e5-siglip") == 0
    assert _migrate("--to", "e5-siglip", "--finish") == 0
    _restart(monkeypatch)
    assert profiles.resolve().name == "e5-siglip"
    with session_scope() as s:
        new = s.execute(select(File).where(File.rel_path == "n.md")).scalar_one().id
    assert any(p.payload.get("file_id") == new for p in _points("chunks"))


def test_migration_needs_a_recorded_profile(env: None) -> None:
    from voitta_rag_enterprise.db.database import init_db

    init_db()
    assert _migrate("--to", "gemma2") == 2


def test_qdrant_payload_has_no_vectors_from_both_models(legacy_install: int) -> None:
    """Belt and braces: after a migration each collection's points carry
    exactly one profile's version stamps."""
    _bulk_and_finish()
    for coll, prof in (("chunks", "e5-siglip"), ("chunks__gemma2", "gemma2")):
        stamps = {p.payload["dense_model_version"] for p in _points(coll)}
        assert stamps == {profiles.profile(prof).dense_version}
