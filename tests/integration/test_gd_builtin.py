"""Built-in Google Drive OAuth client (desktop "Sign in (no setup)" mode).

The vendor ships a Desktop-type GCP client via
``VOITTA_GD_BUILTIN_CLIENT_ID/SECRET``; a source row opts in with
``gd_use_builtin`` and carries no credentials of its own. These tests lock
in the gating (single-user only), the credential substitution at every
read site, and that the baked secret never leaks into API responses.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import FolderSyncSource

BUILTIN_ID = "builtin-cid.apps.googleusercontent.com"
BUILTIN_SECRET = "GOCSPX-builtin-secret-value"


@pytest.fixture
def builtin_env(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-user deploy with a built-in Drive client configured."""
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    monkeypatch.setenv("VOITTA_GD_BUILTIN_CLIENT_ID", BUILTIN_ID)
    monkeypatch.setenv("VOITTA_GD_BUILTIN_CLIENT_SECRET", BUILTIN_SECRET)
    reset_settings_cache()


def _app():
    from voitta_rag_enterprise.main import create_app

    return create_app()


def _register_folder(client: TestClient, tmp_path: Path) -> int:
    src = tmp_path / "drive"
    src.mkdir(exist_ok=True)
    r = client.post("/api/folders", json={"name": src.name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _put_builtin(client: TestClient, fid: int) -> object:
    return client.put(
        f"/api/folders/{fid}/sync",
        json={
            "source_type": "google_drive",
            "google_drive": {"use_builtin": True},
        },
    )


# ---------------------------------------------------------------------------
# Capability flag
# ---------------------------------------------------------------------------


def test_me_reports_builtin_available(builtin_env: None) -> None:
    with TestClient(_app()) as c:
        me = c.get("/api/auth/me").json()
        assert me["gd_builtin_available"] is True


def test_me_reports_unavailable_without_env(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    reset_settings_cache()
    with TestClient(_app()) as c:
        assert c.get("/api/auth/me").json()["gd_builtin_available"] is False


def test_me_reports_unavailable_multi_user(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Creds present but multi-user deploy → not offered."""
    from voitta_rag_enterprise.config import reset_settings_cache

    from ..conftest import auth_as

    monkeypatch.setenv("VOITTA_GD_BUILTIN_CLIENT_ID", BUILTIN_ID)
    monkeypatch.setenv("VOITTA_GD_BUILTIN_CLIENT_SECRET", BUILTIN_SECRET)
    reset_settings_cache()
    app = _app()
    auth_as(app, "alice@x")
    with TestClient(app) as c:
        assert c.get("/api/auth/me").json()["gd_builtin_available"] is False
        # ...and the PUT is refused outright.
        fid = _register_folder(c, tmp_path)
        r = _put_builtin(c, fid)
        assert r.status_code == 400
        assert "not available" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Save / round-trip / redaction
# ---------------------------------------------------------------------------


def test_put_builtin_saves_without_creds(builtin_env: None, tmp_path: Path) -> None:
    with TestClient(_app()) as c:
        fid = _register_folder(c, tmp_path)
        r = _put_builtin(c, fid)
        assert r.status_code == 200, r.text

        out = c.get(f"/api/folders/{fid}/sync")
        assert out.status_code == 200
        gd = out.json()["google_drive"]
        assert gd["use_builtin"] is True
        # The shipped client's identity never reaches the SPA.
        assert gd["client_id"] == ""
        assert gd["has_client_secret"] is False
        assert gd["connected"] is False
        assert gd["use_loopback"] is False
        body = out.text
        assert BUILTIN_SECRET not in body
        assert BUILTIN_ID not in body

    with session_scope() as s:
        src = s.get(FolderSyncSource, fid)
        assert src.gd_use_builtin is True
        assert src.gd_client_id is None
        assert src.gd_client_secret is None


def test_auth_init_uses_builtin_client(builtin_env: None, tmp_path: Path) -> None:
    with TestClient(_app()) as c:
        fid = _register_folder(c, tmp_path)
        assert _put_builtin(c, fid).status_code == 200
        r = c.post(f"/api/folders/{fid}/sync/google-drive/auth")
        assert r.status_code == 200, r.text
        auth_url = r.json()["auth_url"]
        assert BUILTIN_ID in auth_url
        # Redirect goes to the request host (desktop = 127.0.0.1), never
        # the hosted loopback-bridge port.
        assert "localhost:53682" not in auth_url


def test_auth_init_400_when_creds_removed(
    builtin_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A builtin row saved earlier must fail loudly once the env is gone."""
    from voitta_rag_enterprise.config import reset_settings_cache

    with TestClient(_app()) as c:
        fid = _register_folder(c, tmp_path)
        assert _put_builtin(c, fid).status_code == 200

        monkeypatch.delenv("VOITTA_GD_BUILTIN_CLIENT_ID")
        monkeypatch.delenv("VOITTA_GD_BUILTIN_CLIENT_SECRET")
        reset_settings_cache()
        r = c.post(f"/api/folders/{fid}/sync/google-drive/auth")
        assert r.status_code == 400
        assert "not available" in r.json()["detail"]


def test_callback_exchanges_with_builtin_creds(
    builtin_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from voitta_rag_enterprise.api.routes.sync import google_drive as sync_routes

    exchange = AsyncMock(return_value={"refresh_token": "rt-from-google"})
    monkeypatch.setattr(sync_routes, "gd_exchange_code", exchange)

    with TestClient(_app()) as c:
        fid = _register_folder(c, tmp_path)
        assert _put_builtin(c, fid).status_code == 200
        state = base64.urlsafe_b64encode(str(fid).encode()).decode()
        r = c.get(
            "/api/sync/oauth/google/callback",
            params={"code": "the-code", "state": state},
        )
        assert r.status_code == 200, r.text

    kwargs = exchange.await_args.kwargs
    assert kwargs["client_id"] == BUILTIN_ID
    assert kwargs["client_secret"] == BUILTIN_SECRET
    with session_scope() as s:
        assert s.get(FolderSyncSource, fid).gd_refresh_token == "rt-from-google"


# ---------------------------------------------------------------------------
# Mode flips and token invalidation
# ---------------------------------------------------------------------------


def test_flipping_builtin_clears_refresh_token(
    builtin_env: None, tmp_path: Path
) -> None:
    with TestClient(_app()) as c:
        fid = _register_folder(c, tmp_path)
        assert _put_builtin(c, fid).status_code == 200
        with session_scope() as s:
            s.get(FolderSyncSource, fid).gd_refresh_token = "rt-builtin"

        # Re-save with the flag unchanged → token survives.
        assert _put_builtin(c, fid).status_code == 200
        with session_scope() as s:
            assert s.get(FolderSyncSource, fid).gd_refresh_token == "rt-builtin"

        # Switch to a user-supplied client → token cleared (it was minted
        # by the built-in client and won't refresh under the new one).
        r = c.put(
            f"/api/folders/{fid}/sync",
            json={
                "source_type": "google_drive",
                "google_drive": {
                    "use_builtin": False,
                    "client_id": "user-cid",
                    "client_secret": "user-secret",
                },
            },
        )
        assert r.status_code == 200, r.text
        with session_scope() as s:
            src = s.get(FolderSyncSource, fid)
            assert src.gd_refresh_token is None
            assert src.gd_use_builtin is False


def test_builtin_forces_loopback_off(builtin_env: None, tmp_path: Path) -> None:
    with TestClient(_app()) as c:
        fid = _register_folder(c, tmp_path)
        r = c.put(
            f"/api/folders/{fid}/sync",
            json={
                "source_type": "google_drive",
                "google_drive": {"use_builtin": True, "use_loopback": True},
            },
        )
        assert r.status_code == 200
        with session_scope() as s:
            assert s.get(FolderSyncSource, fid).gd_use_loopback is False


# ---------------------------------------------------------------------------
# Connector credential substitution
# ---------------------------------------------------------------------------


def test_resolve_config_substitutes_builtin_creds(builtin_env: None) -> None:
    from voitta_rag_enterprise.services.sync.google_drive import (
        GoogleDriveConnector,
    )

    row = FolderSyncSource(
        folder_id=1,
        source_type="google_drive",
        gd_use_builtin=True,
        gd_refresh_token="rt",
    )
    cfg = GoogleDriveConnector().resolve_config(row)
    assert cfg["auth"].client_id == BUILTIN_ID
    assert cfg["auth"].client_secret == BUILTIN_SECRET
    assert cfg["auth"].refresh_token == "rt"


def test_resolve_config_raises_when_unavailable(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desktop DB imported into a hosted (multi-user) deploy: the built-in
    creds must never be used there, even if the env vars are set."""
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.services.sync.google_drive import (
        GoogleDriveConnector,
    )

    monkeypatch.setenv("VOITTA_GD_BUILTIN_CLIENT_ID", BUILTIN_ID)
    monkeypatch.setenv("VOITTA_GD_BUILTIN_CLIENT_SECRET", BUILTIN_SECRET)
    reset_settings_cache()

    row = FolderSyncSource(
        folder_id=1,
        source_type="google_drive",
        gd_use_builtin=True,
        gd_refresh_token="rt",
    )
    with pytest.raises(RuntimeError, match="not available"):
        GoogleDriveConnector().resolve_config(row)
