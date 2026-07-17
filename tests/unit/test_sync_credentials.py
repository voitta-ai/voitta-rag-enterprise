"""Company-scoped reusable sync credentials: CRUD, company boundary,
folder references, and auth resolution through resolve_gd_auth.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import auth_as

SA_JSON = json.dumps({"type": "service_account", "client_email": "sa@p.iam"})


def auth_as_company(app: FastAPI, email: str, company_id: str) -> int:
    """Like conftest.auth_as, but with a distinct company scope — the
    dev-user harness puts everyone in company '' which would defeat the
    boundary assertions."""
    from voitta_rag_enterprise.api.deps import current_user, real_user
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.services.acl import CurrentUser, get_or_create_user

    with session_scope() as s:
        user = get_or_create_user(s, email)
        s.commit()
        uid, mail = user.id, user.email

    fake = lambda: CurrentUser(id=uid, email=mail, company_id=company_id)  # noqa: E731
    app.dependency_overrides[current_user] = fake
    app.dependency_overrides[real_user] = fake
    return uid


def _mk_oauth(client: TestClient, label: str = "corp client") -> dict:
    r = client.post(
        "/api/sync/credentials",
        json={
            "kind": "google_oauth_client",
            "label": label,
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# CRUD + masking
# ---------------------------------------------------------------------------


def test_create_list_masks_secrets(app: FastAPI, client: TestClient) -> None:
    auth_as(app, "a@corp.com")
    created = _mk_oauth(client)
    assert created["has_client_secret"] is True
    assert created["connected"] is False
    assert "client_secret" not in created

    listed = client.get("/api/sync/credentials").json()
    assert [c["id"] for c in listed] == [created["id"]]
    assert listed[0]["client_id"] == "cid.apps.googleusercontent.com"
    assert listed[0]["in_use_by"] == 0


def test_create_service_account_validates_json(
    app: FastAPI, client: TestClient
) -> None:
    auth_as(app, "a@corp.com")
    bad = client.post(
        "/api/sync/credentials",
        json={"kind": "google_service_account", "service_account_json": "{not json"},
    )
    assert bad.status_code == 400

    wrong_type = client.post(
        "/api/sync/credentials",
        json={
            "kind": "google_service_account",
            "service_account_json": json.dumps({"type": "authorized_user"}),
        },
    )
    assert wrong_type.status_code == 400

    ok = client.post(
        "/api/sync/credentials",
        json={"kind": "google_service_account", "service_account_json": SA_JSON},
    )
    assert ok.status_code == 200
    assert ok.json()["has_service_account"] is True


def test_oauth_kind_requires_client_pair(app: FastAPI, client: TestClient) -> None:
    auth_as(app, "a@corp.com")
    r = client.post(
        "/api/sync/credentials",
        json={"kind": "google_oauth_client", "client_id": "cid-only"},
    )
    assert r.status_code == 400


def test_delete_unreferenced(app: FastAPI, client: TestClient) -> None:
    auth_as(app, "a@corp.com")
    cred = _mk_oauth(client)
    assert client.delete(f"/api/sync/credentials/{cred['id']}").status_code == 204
    assert client.get("/api/sync/credentials").json() == []


# ---------------------------------------------------------------------------
# Company boundary
# ---------------------------------------------------------------------------


def test_credentials_scoped_to_company(app: FastAPI, client: TestClient) -> None:
    auth_as_company(app, "a@corp.com", "org_corp")
    cred = _mk_oauth(client)

    auth_as_company(app, "b@other.com", "org_other")
    assert client.get("/api/sync/credentials").json() == []
    assert client.delete(f"/api/sync/credentials/{cred['id']}").status_code == 404
    assert (
        client.post(f"/api/sync/credentials/{cred['id']}/google/auth").status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Folder references
# ---------------------------------------------------------------------------


def _mk_folder(client: TestClient, name: str = "synced") -> int:
    r = client.post("/api/folders", json={"name": name, "display_name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _put_sync_with_credential(
    client: TestClient, folder_id: int, cred_id: int
) -> dict:
    r = client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "google_drive",
            "google_drive": {"credential_id": cred_id},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_folder_sync_references_credential(app: FastAPI, client: TestClient) -> None:
    auth_as(app, "a@corp.com")
    cred = _mk_oauth(client)
    folder_id = _mk_folder(client)

    out = _put_sync_with_credential(client, folder_id, cred["id"])
    gd = out["google_drive"]
    assert gd["credential_id"] == cred["id"]
    # Resolved view: the credential has a secret but no consent yet.
    assert gd["has_client_secret"] is True
    assert gd["connected"] is False

    # The reference shows up in the credential listing…
    assert client.get("/api/sync/credentials").json()[0]["in_use_by"] == 1
    # …and blocks deletion.
    assert client.delete(f"/api/sync/credentials/{cred['id']}").status_code == 409


def test_folder_sync_rejects_foreign_credential(
    app: FastAPI, client: TestClient
) -> None:
    auth_as_company(app, "a@corp.com", "org_corp")
    cred = _mk_oauth(client)

    auth_as_company(app, "b@other.com", "org_other")
    folder_id = _mk_folder(client, "other-co")
    r = client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "google_drive",
            "google_drive": {"credential_id": cred["id"]},
        },
    )
    assert r.status_code == 404


def test_folder_auth_init_refused_for_credential_rows(
    app: FastAPI, client: TestClient
) -> None:
    auth_as(app, "a@corp.com")
    cred = _mk_oauth(client)
    folder_id = _mk_folder(client)
    _put_sync_with_credential(client, folder_id, cred["id"])

    r = client.post(f"/api/folders/{folder_id}/sync/google-drive/auth")
    assert r.status_code == 400
    assert "shared company credential" in r.json()["detail"]


def test_credential_auth_init_returns_google_url(
    app: FastAPI, client: TestClient
) -> None:
    auth_as(app, "a@corp.com")
    cred = _mk_oauth(client)
    r = client.post(f"/api/sync/credentials/{cred['id']}/google/auth")
    assert r.status_code == 200
    url = r.json()["auth_url"]
    assert url.startswith("https://accounts.google.com/")
    assert "cid.apps.googleusercontent.com" in url


def test_switching_to_inline_clears_reference(
    app: FastAPI, client: TestClient
) -> None:
    auth_as(app, "a@corp.com")
    cred = _mk_oauth(client)
    folder_id = _mk_folder(client)
    _put_sync_with_credential(client, folder_id, cred["id"])

    out = client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "google_drive",
            "google_drive": {
                "client_id": "inline-cid",
                "client_secret": "inline-secret",
            },
        },
    ).json()
    assert out["google_drive"]["credential_id"] is None
    assert out["google_drive"]["client_id"] == "inline-cid"
    assert client.get("/api/sync/credentials").json()[0]["in_use_by"] == 0


# ---------------------------------------------------------------------------
# Import (promote) inline folder credentials into the registry
# ---------------------------------------------------------------------------


def _put_inline_sync(client: TestClient, folder_id: int) -> None:
    r = client.put(
        f"/api/folders/{folder_id}/sync",
        json={
            "source_type": "google_drive",
            "google_drive": {
                "client_id": "inline-cid.apps.googleusercontent.com",
                "client_secret": "inline-secret",
            },
        },
    )
    assert r.status_code == 200, r.text


def test_import_from_folder_carries_consent_and_repoints(
    app: FastAPI, client: TestClient
) -> None:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import FolderSyncSource

    auth_as(app, "a@corp.com")
    folder_id = _mk_folder(client)
    _put_inline_sync(client, folder_id)

    # Simulate a prior consent stored inline (pre-registry state).
    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        assert src is not None
        src.gd_refresh_token = "rt-legacy"

    r = client.post(f"/api/sync/credentials/import-from-folder/{folder_id}")
    assert r.status_code == 200, r.text
    cred = r.json()
    assert cred["kind"] == "google_oauth_client"
    assert cred["client_id"] == "inline-cid.apps.googleusercontent.com"
    assert cred["has_client_secret"] is True
    assert cred["connected"] is True  # consent carried over
    assert cred["in_use_by"] == 1  # owner import re-points the folder

    # Folder now references the credential; inline fields are cleared.
    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        assert src is not None
        assert src.gd_credential_id == cred["id"]
        assert src.gd_client_id is None
        assert src.gd_refresh_token is None

    # Envelope still reports connected — resolved through the credential.
    gd = client.get(f"/api/folders/{folder_id}/sync").json()["google_drive"]
    assert gd["credential_id"] == cred["id"]
    assert gd["connected"] is True


def test_import_rejects_folder_without_inline_creds(
    app: FastAPI, client: TestClient
) -> None:
    auth_as(app, "a@corp.com")
    folder_id = _mk_folder(client)
    r = client.post(f"/api/sync/credentials/import-from-folder/{folder_id}")
    assert r.status_code == 400

    # Already-promoted folders can't be imported twice.
    _put_inline_sync(client, folder_id)
    first = client.post(f"/api/sync/credentials/import-from-folder/{folder_id}")
    assert first.status_code == 200
    again = client.post(f"/api/sync/credentials/import-from-folder/{folder_id}")
    assert again.status_code == 400


def test_import_invisible_folder_404s(app: FastAPI, client: TestClient) -> None:
    auth_as(app, "a@corp.com")
    folder_id = _mk_folder(client, "private-a")
    _put_inline_sync(client, folder_id)

    auth_as(app, "b@other.com")
    r = client.post(f"/api/sync/credentials/import-from-folder/{folder_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# resolve_gd_auth — the seam the pickers and the sync connector read through
# ---------------------------------------------------------------------------


def test_resolve_gd_auth_prefers_credential(app: FastAPI, client: TestClient) -> None:
    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import FolderSyncSource, SyncCredential
    from voitta_rag_enterprise.services.sync.google_drive import resolve_gd_auth

    auth_as(app, "a@corp.com")
    cred = _mk_oauth(client)
    folder_id = _mk_folder(client)
    _put_sync_with_credential(client, folder_id, cred["id"])

    # Grant consent directly on the credential (as the OAuth callback would).
    with session_scope() as s:
        row = s.get(SyncCredential, cred["id"])
        assert row is not None
        row.refresh_token = "rt-from-credential"

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        assert src is not None
        resolved = resolve_gd_auth(src)
    assert resolved.client_id == "cid.apps.googleusercontent.com"
    assert resolved.client_secret == "GOCSPX-secret"
    assert resolved.refresh_token == "rt-from-credential"
    assert resolved.connected

    # Every folder referencing the credential is now connected in the
    # envelope view too — one consent, many folders.
    gd = client.get(f"/api/folders/{folder_id}/sync").json()["google_drive"]
    assert gd["connected"] is True


def test_resolve_gd_auth_dangling_reference_raises(
    app: FastAPI, client: TestClient
) -> None:
    import pytest

    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import FolderSyncSource
    from voitta_rag_enterprise.services.sync.google_drive import resolve_gd_auth

    auth_as(app, "a@corp.com")
    cred = _mk_oauth(client)
    folder_id = _mk_folder(client)
    _put_sync_with_credential(client, folder_id, cred["id"])

    # Point the row at a credential id that doesn't exist. Fresh DBs enforce
    # the FK (dangling impossible), but migrated DBs added the column without
    # one — simulate that legacy shape by disabling FK checks for this write.
    from sqlalchemy import text

    with session_scope() as s:
        s.execute(text("PRAGMA foreign_keys=OFF"))
        src = s.get(FolderSyncSource, folder_id)
        assert src is not None
        src.gd_credential_id = 999_999

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        assert src is not None
        with pytest.raises(RuntimeError, match="no longer exists"):
            resolve_gd_auth(src)

    # The envelope GET degrades gracefully instead of 500ing.
    gd = client.get(f"/api/folders/{folder_id}/sync").json()["google_drive"]
    assert gd["connected"] is False
    assert gd["has_client_secret"] is False
