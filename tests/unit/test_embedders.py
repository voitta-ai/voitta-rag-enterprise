"""Tests for the fake embedders + factory selection."""

from __future__ import annotations

import pytest

from voitta_image_rag.services.embedding import (
    SparseVector,
    get_image_embedder,
    get_sparse_embedder,
    get_text_embedder,
)
from voitta_image_rag.services.embedding.image import FakeImageEmbedder
from voitta_image_rag.services.embedding.sparse import FakeSparseEmbedder
from voitta_image_rag.services.embedding.text import FakeTextEmbedder


def test_factory_returns_fake_in_test_env(env: None) -> None:
    assert isinstance(get_text_embedder(), FakeTextEmbedder)
    assert isinstance(get_sparse_embedder(), FakeSparseEmbedder)
    assert isinstance(get_image_embedder(), FakeImageEmbedder)


def test_text_embedder_is_deterministic(env: None) -> None:
    e = get_text_embedder()
    a = e.embed_query("hello world")
    b = e.embed_query("hello world")
    c = e.embed_query("different text")
    assert a == b
    assert a != c
    assert len(a) == e.dim


def test_text_embedder_l2_normalised(env: None) -> None:
    e = get_text_embedder()
    v = e.embed_query("anything")
    norm_sq = sum(x * x for x in v)
    assert norm_sq == pytest.approx(1.0, rel=1e-6)


def test_text_embedder_batches(env: None) -> None:
    e = get_text_embedder()
    vs = e.embed_documents(["a", "b", "c"])
    assert len(vs) == 3
    assert all(len(v) == e.dim for v in vs)


def test_sparse_embedder_emits_token_indices(env: None) -> None:
    e = get_sparse_embedder()
    sv = e.embed_query("the quick brown fox jumps over the lazy dog")
    assert isinstance(sv, SparseVector)
    assert len(sv.indices) == len(sv.values) > 0
    assert all(0 <= i < FakeSparseEmbedder.VOCAB for i in sv.indices)


def test_sparse_embedder_batches(env: None) -> None:
    e = get_sparse_embedder()
    out = e.embed_documents(["alpha beta", "gamma delta epsilon"])
    assert len(out) == 2


def test_sparse_embedder_empty_text(env: None) -> None:
    e = get_sparse_embedder()
    sv = e.embed_query("!!!")
    assert sv.indices == []
    assert sv.values == []


def test_image_embedder_text_and_image_have_same_dim(env: None) -> None:
    e = get_image_embedder()
    vec_text = e.embed_text("a red logo")
    vec_image = e.embed_image(b"\x89PNG fake bytes")
    assert len(vec_text) == len(vec_image) == e.dim
