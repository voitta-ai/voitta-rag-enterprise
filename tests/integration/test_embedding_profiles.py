"""Embedding profile resolution and the vector-space safety check.

Real SQLite and embedded Qdrant; fake embedders. See
``services/embedding/profiles.py`` for the rules under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from qdrant_client.http import models as qm

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import CollectionMeta
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
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    reset_embedder_caches()
    vs.reset_client_cache()


def _legacy_collection_with_a_point(name: str = "chunks", dim: int = 64) -> None:
    """What a pre-profile install looks like: an unsuffixed collection, no meta row."""

    def _do() -> None:
        c = vs.get_client()
        c.create_collection(name, vectors_config={"dense": qm.VectorParams(size=dim, distance=qm.Distance.COSINE)})
        c.upsert(name, points=[qm.PointStruct(id=1, vector={"dense": [1.0] + [0.0] * (dim - 1)}, payload={})])

    vs.run_on_qdrant(_do)


def _meta() -> dict[str, str]:
    with session_scope() as s:
        return {m.collection_name: m.profile for m in s.query(CollectionMeta)}


# ---------------------------------------------------------------------------
# resolution order
# ---------------------------------------------------------------------------


def test_fresh_install_uses_the_install_default(env: None) -> None:
    init_db()
    r = profiles.resolve()
    assert (r.name, r.source) == ("gemma2", "default")
    assert r.profile.chunks_collection == "chunks__gemma2"
    assert vs.chunks_collection() == "chunks__gemma2"
    assert vs.images_collection() == "images__gemma2"


def test_desktop_default_is_e5_siglip(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh(monkeypatch, VOITTA_DEFAULT_EMBEDDING_PROFILE="e5-siglip")
    init_db()
    r = profiles.resolve()
    assert (r.name, r.source) == ("e5-siglip", "default")
    assert vs.chunks_collection() == "chunks"


def test_env_beats_stored_beats_default(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    profiles.write_stored_profile("e5-siglip")
    profiles.reset_profile_cache()
    assert (profiles.resolve().name, profiles.resolve().source) == ("e5-siglip", "app_settings")
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="gemma2")
    assert (profiles.resolve().name, profiles.resolve().source) == ("gemma2", "env")


# ---------------------------------------------------------------------------
# existing installs
# ---------------------------------------------------------------------------


def test_upgraded_install_adopts_its_legacy_profile(env: None) -> None:
    """Saturn's case: no env, no stored profile, data only in e5 collections."""
    init_db()
    _legacy_collection_with_a_point("chunks")
    r = profiles.resolve()
    assert (r.name, r.source) == ("e5-siglip", "adopted")
    assert "migrate_embedding_profile.py --to gemma2" in r.note
    # The unsuffixed collection is now on record as e5-siglip.
    assert _meta() == {"chunks": "e5-siglip"}
    assert vs.chunks_collection() == "chunks"


def test_explicit_profile_against_other_profiles_data_refuses(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_db()
    _legacy_collection_with_a_point("chunks")
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="gemma2")
    with pytest.raises(ProfileMismatchError, match="VOITTA_EMBEDDING_PROFILE=e5-siglip"):
        profiles.resolve()


def test_stored_profile_against_other_profiles_data_refuses(env: None) -> None:
    init_db()
    _legacy_collection_with_a_point("images")
    profiles.write_stored_profile("gemma2")
    with pytest.raises(ProfileMismatchError, match="migrate_embedding_profile.py --to gemma2"):
        profiles.resolve()


def test_migrated_install_runs_its_new_profile(env: None) -> None:
    """After a migration both profiles' collections hold data; the stored
    choice wins and the old collections are left alone for rollback."""
    init_db()
    _legacy_collection_with_a_point("chunks")
    _legacy_collection_with_a_point("chunks__gemma2")
    profiles.write_stored_profile("gemma2")
    r = profiles.resolve()
    assert (r.name, r.source) == ("gemma2", "app_settings")


def test_collection_recorded_under_another_profile_refuses(env: None) -> None:
    init_db()
    profiles.record_collection(
        "chunks__gemma2", profiles.profile("e5-siglip"), dim=768, kind="chunks"
    )
    with pytest.raises(ProfileMismatchError, match="chunks__gemma2"):
        profiles.resolve()


def test_app_refuses_to_start_on_a_mismatch(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.main import create_app

    init_db()
    _legacy_collection_with_a_point("chunks")
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="gemma2")
    with pytest.raises(ProfileMismatchError), TestClient(create_app()):
        pass


# ---------------------------------------------------------------------------
# collections and embedders follow the profile
# ---------------------------------------------------------------------------


def test_ensure_records_which_profile_built_a_collection(env: None) -> None:
    init_db()
    vs.ensure_chunks_collection(text_dim=64)
    vs.ensure_images_collection(image_dim=64)
    assert _meta() == {"chunks__gemma2": "gemma2", "images__gemma2": "gemma2"}
    with session_scope() as s:
        row = s.get(CollectionMeta, "chunks__gemma2")
        assert (row.dense_model, row.image_model, row.dim) == ("google/embeddinggemma-2", None, 64)


def test_overrides_apply_and_unified_models_cannot_split(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fresh(monkeypatch, VOITTA_DENSE_VERSION="embeddinggemma-2@7")
    assert profiles.profile("gemma2").dense_version == "embeddinggemma-2@7"
    _fresh(monkeypatch, VOITTA_DENSE_MODEL="a/model", VOITTA_IMAGE_MODEL="b/model")
    with pytest.raises(ValueError, match="one model"):
        profiles.profile("gemma2")
    # e5-siglip has two models; overriding one is fine.
    assert profiles.profile("e5-siglip").image_model == "b/model"


def test_unknown_profile_is_rejected(env: None) -> None:
    with pytest.raises(ValueError, match="unknown embedding profile"):
        profiles.pin("e7")


# ---------------------------------------------------------------------------
# indexing and search end to end, per profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", ["gemma2", "e5-siglip"])
def test_index_and_search_land_in_the_profiles_collections(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile_name: str
) -> None:
    from voitta_rag_enterprise.main import create_app

    _fresh(monkeypatch, VOITTA_DEFAULT_EMBEDDING_PROFILE=profile_name, VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:
        _seed_and_index(client, tmp_path / "docs", {"a.md": "alpha beta gamma", "p.png": _png()})
        prof = profiles.active()
        counts = vs.collection_point_counts(list(profiles.ALL_COLLECTIONS))
        assert counts[prof.chunks_collection] >= 1
        assert counts[prof.images_collection] == 1
        others = set(profiles.ALL_COLLECTIONS) - {prof.chunks_collection, prof.images_collection}
        assert all(counts[c] == 0 for c in others)

        def payload_versions():
            pts, _ = vs.get_client().scroll(prof.chunks_collection, limit=10, with_payload=True)
            img, _ = vs.get_client().scroll(prof.images_collection, limit=10, with_payload=True)
            return {p.payload["dense_model_version"] for p in pts}, {p.payload["image_model_version"] for p in img}

        dense_v, image_v = vs.run_on_qdrant(payload_versions)
        assert dense_v == {prof.dense_version}
        assert image_v == {prof.image_version}

        r = client.post("/api/search", json={"query": "alpha beta", "modes": ["chunks", "images"]})
        assert r.status_code == 200, r.text
        assert r.json()["chunks"]


def test_search_embeds_the_query_once_under_gemma2(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_search_before_anything_is_indexed_returns_no_hits(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install has no collections until the first embed; searching
    then must return empty lists, not a 500."""
    from voitta_rag_enterprise.main import create_app

    _fresh(monkeypatch, VOITTA_DEV_USER="test@localhost")
    with TestClient(create_app()) as client:
        r = client.post("/api/search", json={"query": "anything", "modes": ["chunks", "images"]})
        assert r.status_code == 200, r.text
        assert r.json()["chunks"] == [] and r.json()["images"] == []


def test_a_migration_in_progress_is_never_picked_up(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restarting the app mid-migration keeps it on the old profile even
    though the new collections already hold some data."""
    init_db()
    _legacy_collection_with_a_point("chunks")
    _legacy_collection_with_a_point("chunks__gemma2")
    profiles.write_setting(profiles.MIGRATION_SETTING_KEY, "gemma2")
    r = profiles.resolve()
    assert (r.name, r.source) == ("e5-siglip", "adopted")
    _fresh(monkeypatch, VOITTA_EMBEDDING_PROFILE="gemma2")
    with pytest.raises(ProfileMismatchError, match="still being migrated"):
        profiles.resolve()
