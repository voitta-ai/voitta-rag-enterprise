"""``GET /api/jobs/recent`` is folder-ACL scoped.

It used to list every tenant's jobs (the WS snapshot filtered, the REST
route didn't). Both now run ``services.job_listing.recent_visible_jobs``;
these tests pin the REST side: a job on a folder the caller can't see is
hidden, the caller's own jobs and folder-less housekeeping jobs are not.
"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import auth_as
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Folder, Job
from voitta_rag_enterprise.services.acl import get_or_create_user


def _folder(owner_email: str, path: str) -> int:
    with session_scope() as s:
        owner = get_or_create_user(s, owner_email)
        f = Folder(path=path, display_name=path.rsplit("/", 1)[-1], owner_id=owner.id)
        s.add(f)
        s.flush()
        return f.id


def _job(kind: str, payload: dict) -> int:
    with session_scope() as s:
        j = Job(
            kind=kind,
            payload=json.dumps(payload),
            state="queued",
            enqueued_at=int(time.time()),
        )
        s.add(j)
        s.flush()
        return j.id


def test_recent_jobs_hide_other_tenants_folders(app: FastAPI, tmp_path) -> None:
    init_db()
    mine = _folder("alice@example.com", str(tmp_path / "alice"))
    theirs = _folder("bob@example.com", str(tmp_path / "bob"))
    own_job = _job("sync", {"folder_id": mine})
    foreign_job = _job("sync", {"folder_id": theirs})
    global_job = _job("gc_cas", {})

    auth_as(app, "alice@example.com")
    with TestClient(app) as client:
        ids = {j["id"] for j in client.get("/api/jobs/recent").json()}

    assert own_job in ids
    assert global_job in ids
    assert foreign_job not in ids


def test_recent_jobs_same_as_ws_snapshot(app: FastAPI, tmp_path) -> None:
    """REST and the WS ``jobs`` snapshot return the same rows."""
    from voitta_rag_enterprise.api.snapshot import build_snapshot
    from voitta_rag_enterprise.db.database import get_session_factory
    from voitta_rag_enterprise.services.acl import visible_folder_ids

    init_db()
    mine = _folder("alice@example.com", str(tmp_path / "alice"))
    theirs = _folder("bob@example.com", str(tmp_path / "bob"))
    _job("sync", {"folder_id": mine})
    _job("sync", {"folder_id": theirs})

    uid = auth_as(app, "alice@example.com")
    with TestClient(app) as client:
        rest_ids = [j["id"] for j in client.get("/api/jobs/recent").json()]

    db = get_session_factory()()
    try:
        frames = build_snapshot(
            db,
            user_id=uid,
            is_admin=False,
            visible=set(visible_folder_ids(db, uid)),
            topics=("jobs",),
        )
    finally:
        db.close()
    ws_ids = [j["id"] for j in frames[0]["items"]]
    assert rest_ids == ws_ids
