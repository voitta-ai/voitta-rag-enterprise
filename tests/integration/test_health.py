"""Smoke tests for app boot and ``/healthz``."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_healthz_returns_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_app_creates_data_dir_on_startup(env: None, tmp_path: Path) -> None:
    """The lifespan should create ``data_dir`` if missing."""
    from voitta_image_rag.config import get_settings
    from voitta_image_rag.main import create_app

    expected = tmp_path / "data"
    assert get_settings().data_dir == expected
    assert not expected.exists()

    app = create_app()
    with TestClient(app):
        assert expected.exists()


def test_static_mount_serves_index(client: TestClient) -> None:
    r = client.get("/static/index.html")
    assert r.status_code == 200
    assert "voitta-image-rag" in r.text
