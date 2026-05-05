"""``list_root_folders`` auth dispatch tests.

We don't hit the real Drive API here — the goal is to lock in *which*
auth path is taken given the saved credentials, since that's the contract
the SPA's Pick button depends on.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import FolderSyncSource
from voitta_rag_enterprise.services.sync import google_drive

from ..conftest import auth_as


def _app():
    from voitta_rag_enterprise.main import create_app

    return create_app()


@pytest.mark.asyncio
async def test_list_root_folders_no_creds_raises(env: None) -> None:
    """Both auth paths empty → clear error, not a Drive call."""
    with pytest.raises(RuntimeError, match="no OAuth refresh_token"):
        await google_drive.list_root_folders()


@pytest.mark.asyncio
async def test_list_root_folders_oauth_path_uses_refresh_token(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OAuth creds → ``_refresh_access_token`` is called, the SA path isn't."""
    refresh_called = AsyncMock(return_value="oauth-token")
    list_called = AsyncMock(
        return_value={"folders": [], "shared_folders": [], "shared_drives": []}
    )
    monkeypatch.setattr(google_drive, "_refresh_access_token", refresh_called)
    monkeypatch.setattr(google_drive, "_list_drive_locations", list_called)
    sa_called = AsyncMock(return_value="sa-token")
    monkeypatch.setattr(google_drive, "_service_account_access_token", sa_called)

    await google_drive.list_root_folders(
        client_id="cid", client_secret="cs", refresh_token="rt",
    )

    refresh_called.assert_awaited_once_with("cid", "cs", "rt")
    sa_called.assert_not_called()
    # has_my_drive=True: we want the user's own folders too.
    list_called.assert_awaited_once_with("oauth-token", has_my_drive=True)


@pytest.mark.asyncio
async def test_list_root_folders_sa_path_skips_my_drive(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service-account creds → token minted via google-auth, My Drive
    section skipped (SAs have no human My Drive)."""
    refresh_called = AsyncMock(return_value="oauth-token")
    list_called = AsyncMock(
        return_value={"folders": [], "shared_folders": [], "shared_drives": []}
    )
    monkeypatch.setattr(google_drive, "_refresh_access_token", refresh_called)
    monkeypatch.setattr(google_drive, "_list_drive_locations", list_called)
    # _service_account_access_token is sync — patch with a regular function.
    monkeypatch.setattr(
        google_drive,
        "_service_account_access_token",
        lambda _json: "sa-token",
    )

    await google_drive.list_root_folders(
        service_account_json='{"client_email":"x@y","private_key":"…"}',
    )

    refresh_called.assert_not_called()
    list_called.assert_awaited_once_with("sa-token", has_my_drive=False)


def test_endpoint_accepts_sa_only(env: None, tmp_path: Path) -> None:
    """``GET /folders/{id}/sync/google-drive/folders`` no longer requires
    ``gd_refresh_token`` — an SA-only config should reach the listing
    function instead of returning 400."""
    app = _app()
    auth_as(app, "alice@x")
    src = tmp_path / "drive"
    src.mkdir()

    with TestClient(app) as client:
        # Register a folder + save an SA-only GD config.
        r = client.post("/api/folders", json={"name": src.name})
        assert r.status_code == 201
        fid = r.json()["id"]
        r = client.put(
            f"/api/folders/{fid}/sync",
            json={
                "source_type": "google_drive",
                "google_drive": {
                    "client_id": "",
                    "client_secret": "",
                    "service_account_json": '{"client_email":"x@y","private_key":"…"}',
                    "folders": [],
                },
            },
        )
        assert r.status_code == 200, r.text

        # Confirm DB row is in SA-only state.
        with session_scope() as s:
            row = s.execute(
                select(FolderSyncSource).where(FolderSyncSource.folder_id == fid)
            ).scalar_one()
            assert row.gd_service_account_json
            assert not row.gd_refresh_token

        # Patch the listing function so we don't hit Google. The route
        # should call through; previously it would 400 with "not connected".
        called: dict = {"sa_json": None}

        async def fake_list(*, client_id, client_secret, refresh_token, service_account_json):
            called["sa_json"] = service_account_json
            return {"folders": [], "shared_folders": [{"id": "abc", "name": "Shared"}], "shared_drives": []}

        with patch.object(google_drive, "list_root_folders", fake_list):
            # The route imports list_root_folders by alias at module load,
            # so patch the route's binding too.
            from voitta_rag_enterprise.api.routes import sync as sync_route

            with patch.object(sync_route, "gd_list_root_folders", fake_list):
                r = client.get(f"/api/folders/{fid}/sync/google-drive/folders")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["folders"] == []  # SA path skips My Drive
        # GdDrivePickEntry adds owner_* and date fields with empty defaults
        # when the underlying call doesn't supply them — assert on the
        # required fields rather than full equality.
        assert len(body["shared_folders"]) == 1
        assert body["shared_folders"][0]["id"] == "abc"
        assert body["shared_folders"][0]["name"] == "Shared"
        # The route forwarded the saved SA JSON to the listing function.
        assert called["sa_json"] == json.loads('"{\\"client_email\\":\\"x@y\\",\\"private_key\\":\\"…\\"}"')


def test_endpoint_surfaces_owner_and_dates(env: None, tmp_path: Path) -> None:
    """Owner email + sharedWithMeTime + modifiedTime flow through the route
    untouched — the picker depends on them to disambiguate same-named
    folders (every team's "Meet Recordings" is somebody else's drive)."""
    app = _app()
    auth_as(app, "alice@x")
    src = tmp_path / "drive"
    src.mkdir()
    with TestClient(app) as client:
        fid = client.post("/api/folders", json={"name": src.name}).json()["id"]
        client.put(
            f"/api/folders/{fid}/sync",
            json={
                "source_type": "google_drive",
                "google_drive": {
                    "client_id": "",
                    "client_secret": "",
                    "service_account_json": '{"client_email":"sa@x","private_key":"…"}',
                    "folders": [],
                },
            },
        )

        async def fake_list(**_kwargs):
            return {
                "folders": [],
                "shared_folders": [
                    {
                        "id": "abc",
                        "name": "Meet Recordings",
                        "owner_email": "alice@agnitio.ai",
                        "owner_name": "Alice",
                        "shared_at": "2026-04-30T12:00:00Z",
                        "modified_at": "2026-05-01T00:00:00Z",
                    }
                ],
                "shared_drives": [],
            }

        from voitta_rag_enterprise.api.routes import sync as sync_route

        with patch.object(sync_route, "gd_list_root_folders", fake_list):
            r = client.get(f"/api/folders/{fid}/sync/google-drive/folders")

    assert r.status_code == 200, r.text
    item = r.json()["shared_folders"][0]
    assert item["owner_email"] == "alice@agnitio.ai"
    assert item["owner_name"] == "Alice"
    assert item["shared_at"].startswith("2026-04-30")
    assert item["modified_at"].startswith("2026-05-01")


def test_endpoint_still_400s_with_no_creds(env: None, tmp_path: Path) -> None:
    """Empty SA + empty refresh_token → clear 400, not a 500 from a stray
    Drive call."""
    app = _app()
    auth_as(app, "alice@x")
    src = tmp_path / "drive"
    src.mkdir()

    with TestClient(app) as client:
        fid = client.post("/api/folders", json={"name": src.name}).json()["id"]
        # Save an OAuth config (so the row exists in google_drive mode) but
        # don't connect — refresh_token stays empty. To make sure the 400
        # branch fires, manually clear both creds in the DB.
        client.put(
            f"/api/folders/{fid}/sync",
            json={
                "source_type": "google_drive",
                "google_drive": {
                    "client_id": "cid",
                    "client_secret": "cs",
                    "service_account_json": "",
                    "folders": [],
                },
            },
        )
        with session_scope() as s:
            row = s.execute(
                select(FolderSyncSource).where(FolderSyncSource.folder_id == fid)
            ).scalar_one()
            row.gd_client_secret = None
            row.gd_refresh_token = None
            s.commit()

        r = client.get(f"/api/folders/{fid}/sync/google-drive/folders")
        assert r.status_code == 400
        assert "Connect via OAuth or save a service-account JSON" in r.json()["detail"]
