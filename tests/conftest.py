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
    monkeypatch.setenv("VOITTA_ROOT_PATH", str(tmp_path))
    monkeypatch.setenv("VOITTA_DISABLE_BACKGROUND", "true")
    monkeypatch.setenv("VOITTA_USE_FAKE_EMBEDDERS", "true")
    monkeypatch.setenv("VOITTA_USE_FAKE_PDF_PARSER", "true")
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


def auth_as(app: FastAPI, email: str) -> int:
    """Make subsequent ``TestClient(app)`` calls act as ``email``.

    Wires up a ``current_user`` dependency override that bypasses session
    cookies and just returns the requested user (creating them if missing).
    Returns the user's id for assertions.

    The override sticks until the app is rebuilt (the ``env`` fixture's
    teardown wipes module state, so each test starts clean). Call this
    again with a different email to switch identities mid-test.

    Background: production code rejects unauthenticated REST calls with
    401 now that header-based identity is gone. Going through the real
    OAuth flow in tests is too heavy, so we patch the dependency directly.
    This is the standard FastAPI testing pattern.
    """
    from voitta_image_rag.api.deps import current_user
    from voitta_image_rag.db.database import session_scope
    from voitta_image_rag.services.acl import CurrentUser, get_or_create_user

    with session_scope() as s:
        user = get_or_create_user(s, email)
        s.commit()
        uid, mail = user.id, user.email

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid, email=mail)
    return uid
