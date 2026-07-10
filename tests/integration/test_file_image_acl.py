"""Cross-tenant authorization on /api/files/* and /api/images/*.

Regression net for a cross-tenant IDOR: the file- and image-content
endpoints resolved rows by integer id and returned them WITHOUT checking
that the caller could see the owning folder. A principal scoped to an
empty account could read/download every indexed file and image in the
deployment by enumerating ids — while the folder-level routes correctly
404'd the same folders.

The fix routes every handler through a folder-visibility check
(``files._require_visible_file`` / the inline guard in ``images``). These
tests pin it for BOTH transports the bug was reachable through — the
session cookie AND an API-key bearer (the scriptable exploit path) — and
prove the legitimate access paths (owner, explicit grant, community share,
single-user) still work.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import (
    ApiKey,
    File,
    FolderAcl,
    Image,
    User,
)
from voitta_rag_enterprise.services.acl import get_or_create_user

from ..conftest import auth_as
from .test_acl_search import _png, _seed_authed

# Every content endpoint hung off a file id. All must enforce the folder ACL.
FILE_SUBPATHS = ["", "/text", "/raw", "/page-images", "/images", "/layout", "/email", "/stl"]


def _ids_for_folder(folder_id: int) -> tuple[int, int]:
    """Return (text_file_id, image_id) for ``folder_id``.

    The seeded folder holds a ``.md`` (has extracted text) and a ``.png``
    (produces an Image row); pick the text file for /text assertions and
    any image in the folder — joined through File so we don't accidentally
    grab the text file, which has no image.
    """
    with session_scope() as s:
        file_id = s.execute(
            select(File.id)
            .where(File.folder_id == folder_id, File.rel_path.like("%.md"))
        ).scalars().first()
        image_id = s.execute(
            select(Image.id)
            .join(File, Image.file_id == File.id)
            .where(File.folder_id == folder_id)
        ).scalars().first()
    assert file_id is not None and image_id is not None, (
        f"seed produced no text file / image for folder {folder_id}"
    )
    return file_id, image_id


def _mint_vk(email: str, company_id: str = "") -> str:
    """A personal key for ``email``'s (email, company_id) account."""
    from voitta_rag_enterprise.api.routes.api_keys import mint_token

    token, prefix, key_hash = mint_token()
    with session_scope() as s:
        acc = get_or_create_user(s, email, company_id)
        s.add(
            ApiKey(
                user_id=acc.id, name="acl-test", prefix=prefix,
                key_hash=key_hash, created_at=int(time.time()),
            )
        )
    return token


def _seed_two_tenants(app, client, tmp_path: Path):
    """alice + bob, each with an indexed doc (text) and an image."""
    a_folder = _seed_authed(
        app, client, tmp_path / "alice",
        {"a.md": "alpha unique alphazoo", "pic.png": _png((10, 20, 30))},
        "alice@x",
    )
    b_folder = _seed_authed(
        app, client, tmp_path / "bob",
        {"b.md": "beta unique betazoo", "pic.png": _png((200, 100, 50))},
        "bob@x",
    )
    a_file, a_img = _ids_for_folder(a_folder)
    b_file, b_img = _ids_for_folder(b_folder)
    return {
        "a_folder": a_folder, "a_file": a_file, "a_img": a_img,
        "b_folder": b_folder, "b_file": b_file, "b_img": b_img,
    }


# ---------------------------------------------------------------------------
# The core regression: a foreign file/image is unreachable across EVERY
# endpoint — via the session cookie.
# ---------------------------------------------------------------------------


def test_foreign_file_blocked_on_every_subpath_cookie(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        t = _seed_two_tenants(app, client, tmp_path)
        auth_as(app, "alice@x")  # alice cannot see bob's folder

        for sub in FILE_SUBPATHS:
            r = client.get(f"/api/files/{t['b_file']}{sub}")
            assert r.status_code == 404, f"{sub or '/'} leaked: {r.status_code}"
            assert r.json()["detail"] == "File not found", sub

        # bob's image, too.
        r = client.get(f"/api/images/{t['b_img']}")
        assert r.status_code == 404
        assert r.json()["detail"] == "Image not found"


def test_owner_still_reads_own_file_and_image(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        t = _seed_two_tenants(app, client, tmp_path)
        auth_as(app, "alice@x")

        meta = client.get(f"/api/files/{t['a_file']}")
        assert meta.status_code == 200
        assert meta.json()["folder_id"] == t["a_folder"]

        text = client.get(f"/api/files/{t['a_file']}/text")
        assert text.status_code == 200
        assert "alpha" in text.text

        # The owner reaches their image bytes (proves the gate passed —
        # a blocked request would be 404 "Image not found" instead).
        img = client.get(f"/api/images/{t['a_img']}")
        assert img.status_code == 200
        assert img.content  # real PNG bytes from CAS


# ---------------------------------------------------------------------------
# The scriptable exploit path from the report: an API-key bearer scoped to
# an EMPTY account enumerating ids. Must be blocked identically.
# ---------------------------------------------------------------------------


def test_foreign_file_blocked_via_api_key(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        t = _seed_two_tenants(app, client, tmp_path)
        app.dependency_overrides.clear()
        # A key for a brand-new account that owns nothing (like the
        # Demo-Company cvk_ in the live finding).
        key = _mint_vk("outsider@x")
        h = {"Authorization": f"Bearer {key}"}

        for sub in FILE_SUBPATHS:
            r = client.get(f"/api/files/{t['a_file']}{sub}", headers=h)
            assert r.status_code == 404, f"API key read {sub or '/'}: {r.status_code}"
        for fid in (t["a_file"], t["b_file"]):
            assert client.get(f"/api/files/{fid}", headers=h).status_code == 404
        assert client.get(f"/api/images/{t['a_img']}", headers=h).status_code == 404
        assert client.get(f"/api/images/{t['b_img']}", headers=h).status_code == 404


# ---------------------------------------------------------------------------
# Legitimate cross-account access still works: explicit grant + share.
# ---------------------------------------------------------------------------


def test_explicit_grant_makes_file_and_image_visible(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        t = _seed_two_tenants(app, client, tmp_path)

        # Grant bob's folder to alice's account.
        with session_scope() as s:
            alice = s.execute(
                select(User).where(User.email == "alice@x")
            ).scalar_one()
            s.add(FolderAcl(folder_id=t["b_folder"], user_id=alice.id))

        auth_as(app, "alice@x")
        assert client.get(f"/api/files/{t['b_file']}").status_code == 200
        assert client.get(f"/api/files/{t['b_file']}/text").status_code == 200
        assert client.get(f"/api/images/{t['b_img']}").status_code == 200


def test_community_shared_folder_is_readable(env: None, tmp_path: Path) -> None:
    """A shared folder is visible to same-community accounts. Both emails
    are native-allowed here, so they share the ``native`` community and
    alice sees bob's shared file."""
    from voitta_rag_enterprise.db.models import Folder
    from voitta_rag_enterprise.main import create_app
    from voitta_rag_enterprise.services import admin_store

    admin_store.add_allowed_user("alice@x")
    admin_store.add_allowed_user("bob@x")
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        t = _seed_two_tenants(app, client, tmp_path)
        with session_scope() as s:
            s.get(Folder, t["b_folder"]).shared = True

        auth_as(app, "alice@x")
        assert client.get(f"/api/files/{t['b_file']}").status_code == 200
        assert client.get(f"/api/images/{t['b_img']}").status_code == 200


# ---------------------------------------------------------------------------
# Single-user mode: no ACL, everything visible (the desktop/local deploy).
# ---------------------------------------------------------------------------


def test_single_user_mode_sees_all_files(
    env: None, tmp_path: Path, monkeypatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        t = _seed_two_tenants(app, client, tmp_path)
        app.dependency_overrides.clear()
        monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
        reset_settings_cache()
        # No auth headers at all — single-user resolves to root@localhost.
        assert client.get(f"/api/files/{t['a_file']}").status_code == 200
        assert client.get(f"/api/files/{t['b_file']}").status_code == 200
        assert client.get(f"/api/images/{t['a_img']}").status_code == 200
        assert client.get(f"/api/images/{t['b_img']}").status_code == 200
