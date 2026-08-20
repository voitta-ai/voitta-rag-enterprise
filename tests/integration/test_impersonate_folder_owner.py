"""Super-admin "view as owner" on a shared folder → re-sync as the owner.

Backs the folder-list tablet (static/js/render/tree.js): on someone-else's
shared folder a super-admin can impersonate the folder's owner and trigger a
re-sync from that identity. The tablet is pure frontend, so these tests pin
the two backend facts it stands on:

- ``GET /api/folders`` exposes ``owner_id`` (a ``users.id``) — the datum the
  tablet feeds straight into the generic impersonate-by-id endpoint.
- A super-admin can ``POST /api/admin/impersonate/{owner_id}`` for an
  arbitrary owner and, now acting AS the owner, pass the owner-gated
  ``/sync/trigger`` that their own identity is refused (403/404 → 200). A
  non-admin is refused the impersonation itself (403).

Impersonation is session-based, so these tests drive the real
``VOITTA_DEV_USER`` identity path (not ``auth_as``, which overrides both
current/real user and would bypass ``acting_as_user_id`` entirely). The app is
built per-test AFTER the env is set: ``cookie_secure`` is read at app-build
time, and a Secure cookie would be dropped over the TestClient's http.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Folder, FolderSyncSource
from voitta_rag_enterprise.services.acl import get_or_create_user


def _build_app(
    real_email: str, monkeypatch: pytest.MonkeyPatch, *, super_admin: bool
) -> FastAPI:
    """App whose real (session) identity is ``real_email`` via the dev path,
    with non-Secure session cookies so impersonation survives across the
    TestClient's http requests."""
    monkeypatch.setenv("VOITTA_DEV_USER", real_email)
    monkeypatch.setenv("VOITTA_SUPER_ADMINS", real_email if super_admin else "")
    monkeypatch.setenv("VOITTA_COOKIE_SECURE", "false")
    reset_settings_cache()
    from voitta_rag_enterprise.main import create_app

    return create_app()


def _seed_shared_folder(owner_email: str) -> tuple[int, int]:
    """A folder owned by ``owner_email``, shared, with a sync source.

    Returns ``(folder_id, owner_id)``.
    """
    init_db()
    with session_scope() as s:
        owner = get_or_create_user(s, owner_email)
        s.flush()
        folder = Folder(
            path="/data/shared-x",
            display_name="Shared X",
            owner_id=owner.id,
            shared=True,
        )
        s.add(folder)
        s.flush()
        # A source with no registered handler: trigger_check is skipped, so
        # the endpoint reaches "enqueue" and returns 200 for the owner.
        s.add(FolderSyncSource(folder_id=folder.id, source_type="test-source"))
        s.commit()
        return folder.id, owner.id


def test_folders_list_exposes_owner_id(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tablet reads ``owner_id`` off each folder row — pin the contract."""
    fid, owner_id = _seed_shared_folder("owner@agnitio.ai")
    app = _build_app("owner@agnitio.ai", monkeypatch, super_admin=False)
    with TestClient(app) as c:
        rows = {f["id"]: f for f in c.get("/api/folders").json()}
    assert rows[fid]["owner_id"] == owner_id
    assert rows[fid]["owned"] is True


def test_super_admin_impersonates_owner_then_resyncs(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fid, owner_id = _seed_shared_folder("owner@agnitio.ai")
    app = _build_app("boss@agnitio.ai", monkeypatch, super_admin=True)

    with TestClient(app) as c:
        # As the super-admin's OWN identity, the owner-gated re-sync is
        # refused: not the owner (403), or not even visible (404).
        before = c.post(f"/api/folders/{fid}/sync/trigger")
        assert before.status_code in (403, 404), before.text

        # The exact call the tablet makes: impersonate the folder's owner
        # by the owner_id carried in the folder row.
        imp = c.post(f"/api/admin/impersonate/{owner_id}")
        assert imp.status_code == 200, imp.text
        assert imp.json()["acting_as_email"] == "owner@agnitio.ai"

        # Now acting AS the owner, the same re-sync succeeds.
        after = c.post(f"/api/folders/{fid}/sync/trigger")
        assert after.status_code == 200, after.text
        assert after.json()["folder_id"] == fid


def test_non_admin_cannot_impersonate_owner(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner_id = _seed_shared_folder("owner@agnitio.ai")
    app = _build_app("nobody@example.co", monkeypatch, super_admin=False)
    with TestClient(app) as c:
        r = c.post(f"/api/admin/impersonate/{owner_id}")
        assert r.status_code == 403, r.text
