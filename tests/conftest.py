"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate every test from the host environment.

    Strips ``VOITTA_*`` vars, points ``VOITTA_DATA_DIR`` at a per-test tmp dir,
    and clears the Settings + engine caches so the new env takes effect.
    """
    from voitta_image_rag.config import reset_settings_cache
    from voitta_image_rag.db.database import reset_engine_cache
    from voitta_image_rag.services.embedding import reset_embedder_caches
    from voitta_image_rag.services.vector_store import reset_client_cache

    for k in list(os.environ):
        if k.startswith("VOITTA_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOITTA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VOITTA_DISABLE_BACKGROUND", "true")
    monkeypatch.setenv("VOITTA_USE_FAKE_EMBEDDERS", "true")
    reset_settings_cache()
    reset_engine_cache()
    reset_embedder_caches()
    reset_client_cache()
    yield
    reset_settings_cache()
    reset_engine_cache()
    reset_embedder_caches()
    reset_client_cache()


@pytest.fixture
def auth_env(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """``env`` plus ``VOITTA_DEV_USER`` so authenticated routes accept requests."""
    from voitta_image_rag.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_DEV_USER", "test@localhost")
    reset_settings_cache()


@pytest.fixture
def app(auth_env: None) -> FastAPI:
    from voitta_image_rag.main import create_app

    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
