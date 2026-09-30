"""services.sync_overview — the assistant's view of sync + indexing state.

Pins the visibility rules: every visible folder reports status and counts;
source configuration and raw sync errors are owner-only; credentials never
appear at all; invisible folders don't exist.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder, FolderSyncSource, Job
from voitta_rag_enterprise.services.acl import get_or_create_user, grant_folder
from voitta_rag_enterprise.services.retrieval import Viewer
from voitta_rag_enterprise.services.sync_overview import (
    file_problems,
    folder_sync_detail,
    recent_jobs_view,
    sync_overview,
)

SECRETS = ("ghp_SECRET_PAT", "jira-SECRET-token", "refresh-SECRET")


def _world(tmp_path: Path) -> dict[str, int]:
    """alice owns a GitHub-synced folder shared with bob; carol owns a
    folder nobody else can see."""
    init_db()
    now = int(time.time())
    with session_scope() as s:
        alice = get_or_create_user(s, "alice@x").id
        bob = get_or_create_user(s, "bob@x").id
        carol = get_or_create_user(s, "carol@x").id
        synced = Folder(path=str(tmp_path / "repo"), display_name="repo", owner_id=alice)
        private = Folder(path=str(tmp_path / "carol"), display_name="carol", owner_id=carol)
        s.add_all([synced, private])
        s.flush()
        s.add(
            FolderSyncSource(
                folder_id=synced.id,
                source_type="github",
                gh_repo="https://github.com/acme/repo",
                gh_pat=SECRETS[0],
                gh_token=SECRETS[2],
                jira_token=SECRETS[1],
                sync_status="error",
                sync_error="clone failed: 403",
                last_synced_at=now - 7200,
                auto_sync_enabled=True,
                auto_sync_hours=6,
            )
        )
        for i, state in enumerate(("indexed", "indexed", "error", "unsupported", "pending")):
            s.add(
                File(
                    folder_id=synced.id,
                    rel_path=f"f{i}.md",
                    added_at=now,
                    last_seen_at=now,
                    state=state,
                    error="boom" if state == "error" else None,
                )
            )
        s.add(
            Job(
                kind="sync",
                payload=json.dumps({"folder_id": synced.id}),
                state="queued",
                dedup_key=f"sync:{synced.id}",
                enqueued_at=now,
            )
        )
        s.add(
            Job(
                kind="sync",
                payload=json.dumps({"folder_id": synced.id}),
                state="error",
                error="clone failed: 403",
                dedup_key=None,
                enqueued_at=now - 7200,
                finished_at=now - 7100,
            )
        )
        grant_folder(s, synced.id, bob)
        return {
            "alice": alice, "bob": bob, "carol": carol,
            "synced": synced.id, "private": private.id,
        }


def test_owner_sees_config_and_errors(env: None, tmp_path: Path) -> None:
    w = _world(tmp_path)
    with session_scope() as s:
        overview = sync_overview(s, Viewer(user_id=w["alice"]))
        detail = folder_sync_detail(s, Viewer(user_id=w["alice"]), w["synced"])

    (row,) = overview.folders
    assert row.folder_id == w["synced"]
    assert row.owned and row.source_type == "github"
    assert row.sync_status == "error" and row.sync_error == "clone failed: 403"
    assert row.sync_in_flight is True
    assert row.auto_sync_enabled and row.next_auto_sync_at is not None
    assert row.files.model_dump() == {
        "total": 5, "indexed": 2, "pending": 1, "in_progress": 0,
        "error": 1, "unsupported": 1,
    }
    assert detail.source_config == {
        "source_type": "github",
        "gh_repo": "https://github.com/acme/repo",
        "gh_path": None,
        "gh_branches": None,
        "gh_all_branches": False,
        "gh_extended": False,
    }


def test_non_owner_sees_status_only(env: None, tmp_path: Path) -> None:
    w = _world(tmp_path)
    with session_scope() as s:
        (row,) = sync_overview(s, Viewer(user_id=w["bob"])).folders
        detail = folder_sync_detail(s, Viewer(user_id=w["bob"]), w["synced"])
    assert not row.owned
    assert row.sync_status == "error"
    assert row.sync_error is None
    assert detail.source_config is None
    assert detail.last_sync_runs == []


def test_invisible_folders_do_not_exist(env: None, tmp_path: Path) -> None:
    w = _world(tmp_path)
    with session_scope() as s:
        ids = {f.folder_id for f in sync_overview(s, Viewer(user_id=w["alice"])).folders}
        assert w["private"] not in ids
        with pytest.raises(ValueError, match="not found"):
            folder_sync_detail(s, Viewer(user_id=w["alice"]), w["private"])
        with pytest.raises(ValueError, match="not found"):
            file_problems(s, Viewer(user_id=w["alice"]), w["private"])


def test_credentials_never_leave(env: None, tmp_path: Path) -> None:
    w = _world(tmp_path)
    with session_scope() as s:
        viewer = Viewer(user_id=w["alice"])
        blob = json.dumps([
            sync_overview(s, viewer).model_dump(),
            folder_sync_detail(s, viewer, w["synced"]).model_dump(),
            recent_jobs_view(s, viewer),
        ])
    for secret in SECRETS:
        assert secret not in blob


def test_file_problems_and_jobs(env: None, tmp_path: Path) -> None:
    w = _world(tmp_path)
    with session_scope() as s:
        viewer = Viewer(user_id=w["alice"])
        problems = file_problems(s, viewer, w["synced"])
        jobs = recent_jobs_view(s, viewer)
    assert [(p.rel_path, p.state, p.error) for p in problems] == [
        ("f2.md", "error", "boom"),
        ("f3.md", "unsupported", None),
    ]
    assert {j["folder_id"] for j in jobs} == {w["synced"]}
