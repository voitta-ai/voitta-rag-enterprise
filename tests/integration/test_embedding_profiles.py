"""Embedding profile resolution and the vector-space safety checks.

Real SQLite and embedded Qdrant; fake embedders. See
``services/embedding/profiles.py`` for the rules under test.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from qdrant_client.http import models as qm

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, CollectionMeta, File, Folder
from voitta_rag_enterprise.services import vector_store as vs
from voitta_rag_enterprise.services.embedding import (
    get_image_embedder,
    get_text_embedder,
    profiles,
    reset_embedder_caches,
)
from voitta_rag_enterprise.services.embedding.profiles import ProfileMismatchError

from .test_search_route import _png, _seed_and_index


@pytest.fixture(autouse=True)
def _app_defaults(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests assert the app's own default rules; a run-level
    ``--embedding-profile`` would change the default under them."""
    monkeypatch.delenv("VOITTA_DEFAULT_EMBEDDING_PROFILE", raising=False)
    reset_settings_cache()
    reset_embedder_caches()


def _fresh(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """What a process restart does to our caches, with ``env`` applied."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    reset_embedder_caches()
    vs.reset_client_cache()


def _collection_with_a_point(name: str, dim: int = 64) -> None:
    def _do() -> None:
        c = vs.get_client()
        c.create_collection(name, vectors_config={"dense": qm.VectorParams(size=dim, distance=qm.Distance.COSINE)})
        c.upsert(name, points=[qm.PointStruct(id=1, vector={"dense": [1.0] + [0.0] * (dim - 1)}, payload={})])

    vs.run_on_qdrant(_do)


def _sql_content() -> None:
    """Indexed content in SQL — what every pre-profile install has."""
    with session_scope() as s:
        s.add(Folder(id=1, path="/tmp/legacy", display_name="legacy"))
        s.flush()
        s.add(File(id=1, folder_id=1, rel_path="a.md", state="indexed"))
        s.flush()
        s.add(Chunk(id=1, file_id=1, chunk_index=0, chunk_hash="h", text="legacy text"))


def _meta() -> dict[str, str]:
    with session_scope() as s:
        return {m.collection_name: m.profile for m in s.query(CollectionMeta)}


# ---------------------------------------------------------------------------
# first start: choose and record
# ---------------------------------------------------------------------------


def test_fresh_install_uses_the_install_default_and_records_it() -> None:
    init_db()
    r = profiles.resolve()
    assert (r.name, r.source) == ("gemma2", "default")
    assert vs.chunks_collection() == "chunks__gemma2"
    assert vs.images_collection() == "images__gemma2"
    assert profiles.read_stored_profile() is None  # resolving alone never writes
    profiles.commit()
    assert profiles.read_stored_profile() == "gemma2"


def test_desktop_default_is_e5_siglip(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh(monkeypatch, VOITTA_DEFAULT_EMBEDDING_PROFILE="e5-siglip")
    init_db()
    r = profiles.resolve()
    assert (r.name, r.source) == ("e5-siglip", "default")
    assert vs.chunks_collection() == "chunks"


def test_env_chooses_on_first_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="e5-siglip")
    init_db()
    profiles.commit()
    assert profiles.read_stored_profile() == "e5-siglip"


def test_the_recorded_profile_does_not_follow_a_changed_default(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    profiles.commit()  # gemma2 recorded
    _fresh(monkeypatch, VOITTA_DEFAULT_EMBEDDING_PROFILE="e5-siglip")
    assert (profiles.resolve().name, profiles.resolve().source) == ("gemma2", "app_settings")


def test_env_against_the_recorded_profile_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review #8: flipping the env var would serve collections nobody kept up to date."""
    init_db()
    profiles.commit()  # gemma2
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="e5-siglip")
    with pytest.raises(ProfileMismatchError, match="migrate_embedding_profile.py --to e5-siglip"):
        profiles.resolve()


# ---------------------------------------------------------------------------
# installs from before profiles existed
# ---------------------------------------------------------------------------


def test_upgraded_install_adopts_e5_siglip() -> None:
    """Saturn's case: no env, nothing recorded, content indexed by e5."""
    init_db()
    _sql_content()
    _collection_with_a_point("chunks")
    r = profiles.resolve()
    assert (r.name, r.source) == ("e5-siglip", "adopted")
    assert "migrate_embedding_profile.py --to gemma2" in r.note
    assert _meta() == {"chunks": "e5-siglip"}  # backfilled
    profiles.commit()
    assert profiles.read_stored_profile() == "e5-siglip"


def test_upgraded_install_whose_qdrant_was_lost_still_adopts_e5_siglip() -> None:
    """Review #2: SQL decides, not what Qdrant happens to contain."""
    init_db()
    _sql_content()
    assert profiles.resolve().name == "e5-siglip"


def test_upgraded_install_with_the_legacy_env_file_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review #1: the old .env.example set all four model/version variables;
    that crashed startup and would have let e5 into the gemma2 profile."""
    _fresh(
        monkeypatch,
        VOITTA_DENSE_MODEL="intfloat/e5-base-v2",
        VOITTA_IMAGE_MODEL="google/siglip2-base-patch16-224",
        VOITTA_DENSE_VERSION="e5-base-v2@1",
        VOITTA_IMAGE_VERSION="siglip2-base@1",
    )
    init_db()
    _sql_content()
    _collection_with_a_point("chunks")
    assert profiles.resolve().name == "e5-siglip"
    gemma = profiles.profile("gemma2")
    assert (gemma.dense_model, gemma.image_model) == ("google/embeddinggemma-2", "google/embeddinggemma-2")
    assert (gemma.dense_version, gemma.image_version) == ("embeddinggemma-2@1", "embeddinggemma-2@1")


def test_e5_siglip_overrides_still_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh(monkeypatch, VOITTA_DENSE_VERSION="e5-base-v2@7", VOITTA_IMAGE_MODEL="x/siglip")
    e5 = profiles.profile("e5-siglip")
    assert (e5.dense_version, e5.image_model) == ("e5-base-v2@7", "x/siglip")


def test_upgraded_install_with_env_gemma2_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    _sql_content()
    _collection_with_a_point("chunks")
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="gemma2")
    with pytest.raises(ProfileMismatchError, match="embedded with e5-siglip"):
        profiles.resolve()


# ---------------------------------------------------------------------------
# collection checks
# ---------------------------------------------------------------------------


def test_collection_recorded_under_another_profile_refuses() -> None:
    init_db()
    profiles.record_collection("chunks__gemma2", profiles.profile("e5-siglip"), dim=64, kind="chunks")
    with pytest.raises(ProfileMismatchError, match="built by profile e5-siglip"):
        profiles.resolve()


def test_collection_built_with_another_model_refuses() -> None:
    """Review #1: the check now compares models, not only profile names."""
    init_db()
    vs.ensure_chunks_collection(text_dim=64)  # records chunks__gemma2 / gemma model
    with session_scope() as s:
        s.get(CollectionMeta, "chunks__gemma2").dense_model = "intfloat/e5-base-v2"
    profiles.reset_profile_cache()
    with pytest.raises(ProfileMismatchError, match="built with intfloat/e5-base-v2"):
        profiles.resolve()


def test_collection_with_another_vector_size_refuses() -> None:
    init_db()
    vs.ensure_chunks_collection(text_dim=64)
    with session_scope() as s:
        s.get(CollectionMeta, "chunks__gemma2").dim = 768
    profiles.reset_profile_cache()
    with pytest.raises(ProfileMismatchError, match="64-d vectors"):
        profiles.resolve()


def test_a_profile_marked_in_migration_is_never_served() -> None:
    init_db()
    profiles.write_settings({profiles.PROFILE_SETTING_KEY: "gemma2", profiles.MIGRATION_SETTING_KEY: "gemma2"})
    with pytest.raises(ProfileMismatchError, match="still being migrated"):
        profiles.resolve()


def test_ensure_records_which_profile_built_a_collection() -> None:
    init_db()
    vs.ensure_chunks_collection(text_dim=64)
    vs.ensure_images_collection(image_dim=64)
    assert _meta() == {"chunks__gemma2": "gemma2", "images__gemma2": "gemma2"}
    with session_scope() as s:
        row = s.get(CollectionMeta, "chunks__gemma2")
        assert (row.dense_model, row.image_model, row.dim) == ("google/embeddinggemma-2", None, 64)


def test_unknown_profile_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown embedding profile"):
        profiles.pin("e7")
    # An unknown value in the environment fails config validation outright.
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="e7")
    with pytest.raises(ValueError, match="gemma2"):
        profiles.resolve()


# ---------------------------------------------------------------------------
# Qdrant unreachable (review #2)
# ---------------------------------------------------------------------------


def test_unreachable_qdrant_fails_resolution_and_search(monkeypatch: pytest.MonkeyPatch) -> None:
    from qdrant_client.http.exceptions import ResponseHandlingException

    _fresh(monkeypatch, VOITTA_QDRANT_MODE="standalone", VOITTA_QDRANT_URL="http://127.0.0.1:9")
    init_db()
    with pytest.raises(ResponseHandlingException):
        profiles.resolve()
    assert profiles._resolved is None  # nothing was decided on an empty view
    profiles.pin("gemma2")
    with pytest.raises(ResponseHandlingException):
        vs.search_chunks([0.0] * 64, None, folder_ids=[1], limit=5)


def test_app_refuses_to_start_on_a_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.main import create_app

    init_db()
    _sql_content()
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="gemma2")
    with pytest.raises(ProfileMismatchError), TestClient(create_app()):
        pass


# ---------------------------------------------------------------------------
# concurrency guards (review #14, #15)
# ---------------------------------------------------------------------------


def test_resolving_on_the_qdrant_thread_raises_instead_of_deadlocking() -> None:
    init_db()
    profiles.reset_profile_cache()
    with pytest.raises(RuntimeError, match="Qdrant thread"):
        vs.run_on_qdrant(profiles.active)


def test_concurrent_first_callers_share_one_embedder() -> None:
    init_db()
    profiles.pin("gemma2")
    reset_embedder_caches()
    got: list = []
    barrier = threading.Barrier(8)

    def grab() -> None:
        barrier.wait()
        got.append(get_text_embedder() if len(got) % 2 else get_image_embedder())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(e) for e in got}) == 1


# ---------------------------------------------------------------------------
# indexing and search end to end, per profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", ["gemma2", "e5-siglip"])
def test_index_and_search_land_in_the_profiles_collections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile_name: str
) -> None:
    from voitta_rag_enterprise.main import create_app

    _fresh(monkeypatch, VOITTA_DEFAULT_EMBEDDING_PROFILE=profile_name, VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:
        assert profiles.read_stored_profile() == profile_name  # recorded at startup
        _seed_and_index(client, tmp_path / "docs", {"a.md": "alpha beta gamma", "p.png": _png()})
        prof = profiles.active()
        counts = vs.collection_point_counts(list(profiles.ALL_COLLECTIONS))
        assert counts[prof.chunks_collection] >= 1
        assert counts[prof.images_collection] == 1
        others = set(profiles.ALL_COLLECTIONS) - {prof.chunks_collection, prof.images_collection}
        assert all(counts[c] == 0 for c in others)

        def payloads():
            pts, _ = vs.get_client().scroll(prof.chunks_collection, limit=10, with_payload=True)
            img, _ = vs.get_client().scroll(prof.images_collection, limit=10, with_payload=True)
            return pts, img

        pts, img = vs.run_on_qdrant(payloads)
        assert {p.payload["dense_model_version"] for p in pts} == {prof.dense_version}
        assert {p.payload["image_model_version"] for p in img} == {prof.image_version}
        file_chunks = [p for p in pts if p.payload.get("kind") != vs.FOLDER_CARD_KIND]
        assert all(len(p.payload["chunk_hash"]) == 64 for p in file_chunks)

        r = client.post("/api/search", json={"query": "alpha beta", "modes": ["chunks", "images"]})
        assert r.status_code == 200, r.text
        assert r.json()["chunks"]


def test_search_embeds_the_query_once_under_gemma2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    _fresh(monkeypatch, VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:
        _seed_and_index(client, tmp_path / "docs", {"a.md": "alpha beta gamma"})
        emb = get_text_embedder()
        assert emb is get_image_embedder()
        calls: list[str] = []
        real_query, real_text = emb.embed_query, emb.embed_text
        monkeypatch.setattr(emb, "embed_query", lambda q: calls.append("query") or real_query(q))
        monkeypatch.setattr(emb, "embed_text", lambda q: calls.append("text") or real_text(q))
        r = client.post("/api/search", json={"query": "alpha", "modes": ["chunks", "images"]})
        assert r.status_code == 200
        assert calls == ["query"]


def test_search_on_a_collection_that_does_not_exist_yet_is_empty() -> None:
    init_db()
    profiles.pin("gemma2")
    assert vs.search_chunks([0.0] * 64, None, folder_ids=[1], limit=5) == []
    assert vs.search_images([0.0] * 64, folder_ids=[1], limit=5) == []
