"""Cross-tenant authorization on the MCP tool surface.

The MCP tools share the bearer-auth seam with /api but have their own
handlers. Every id-taking tool (get_file, get_chunk_range, get_chunk_images,
get_image, list_page_images, get_page_image, get_page_layout, get_workbook,
list_assets, request_asset, resolve_url) resolved rows by id WITHOUT a
folder-visibility check — so a company/personal key scoped to one account
could read or download any file, chunk, image, or (via request_asset's
signed URL) raw bytes in the deployment by enumerating ids. Only ``search``
was scoped.

The fix routes every id-taking tool through
``mcp_server._require_visible_file`` (or a folder filter, for the
list-returning ``resolve_url``), keyed on the caller's account id from the
same ContextVar the auth middleware sets. These tests drive the tools
directly with that ContextVar pinned to one tenant and assert foreign
resources are unreachable while own resources still resolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from tests.integration.seeding import png, seed_owned
from voitta_rag_enterprise import mcp_server
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import FolderAcl, User
from voitta_rag_enterprise.mcp_server import (
    get_chunk_images,
    get_chunk_range,
    get_file,
    get_image,
    get_page_layout,
    list_assets,
    list_page_images,
    request_asset,
    resolve_url,
)
from voitta_rag_enterprise.services.acl import get_or_create_user


class _as_account:
    """Pin mcp_server._current_user to (email, account_id) for the block —
    the same ContextVar BearerAuthMiddleware sets per request."""

    def __init__(self, email: str) -> None:
        with session_scope() as s:
            self._ctx = (email, get_or_create_user(s, email).id)

    def __enter__(self):
        self._token = mcp_server._current_user.set(self._ctx)
        return self

    def __exit__(self, *exc):
        mcp_server._current_user.reset(self._token)


def _two_tenants(tmp_path: Path) -> dict:
    a = seed_owned(
        tmp_path / "alice", "alice@x",
        {"a.md": "alpha unique content here", "pa.png": png((1, 2, 3))},
        source_url="https://drive.example/alice-doc",
    )
    b = seed_owned(
        tmp_path / "bob", "bob@x",
        {"b.md": "beta secret content here", "pb.png": png((9, 8, 7))},
        source_url="https://drive.example/bob-doc",
    )
    return {"a": a, "b": b}


# ---------------------------------------------------------------------------
# Cross-tenant: every id-taking tool blocks a foreign resource.
# ---------------------------------------------------------------------------


def test_foreign_resources_blocked_across_all_tools(env: None, tmp_path: Path) -> None:
    t = _two_tenants(tmp_path)
    b = t["b"]  # bob's ids; alice is the caller

    with _as_account("alice@x"):
        # get_file / get_chunk_range / layout / list_assets — "not found".
        with pytest.raises(ValueError, match="not found"):
            get_file(b["file_id"])
        with pytest.raises(ValueError, match="not found"):
            get_chunk_range(b["file_id"], 0, 5)
        with pytest.raises(ValueError, match="not found"):
            get_page_layout(b["file_id"], 1)
        with pytest.raises(ValueError, match="not found"):
            list_assets(b["file_id"])
        with pytest.raises(ValueError, match="not found"):
            list_page_images(b["file_id"])
        # request_asset — the signed-URL minter; must refuse before minting.
        with pytest.raises(ValueError, match="not found"):
            request_asset(b["file_id"], "original")
        # image + chunk-image tools resolve through the owning file.
        if b["image_id"] is not None:
            with pytest.raises(ValueError, match="not found"):
                get_image(b["image_id"])
        if b["chunk_id"] is not None:
            with pytest.raises(ValueError, match="not found"):
                get_chunk_images(b["chunk_id"])
        # resolve_url — must not reveal bob's file via its source URL.
        assert resolve_url("https://drive.example/bob-doc") == []


# ---------------------------------------------------------------------------
# Own resources still resolve (the fix doesn't over-block).
# ---------------------------------------------------------------------------


def test_own_resources_resolve(env: None, tmp_path: Path) -> None:
    t = _two_tenants(tmp_path)
    a = t["a"]

    with _as_account("alice@x"):
        doc = get_file(a["file_id"])
        assert "alpha" in doc["text"]
        assert get_chunk_range(a["file_id"], 0, 5)  # non-empty
        assert list_assets(a["file_id"])            # original + md specs
        # request_asset mints a URL for a file the caller owns.
        asset = request_asset(a["file_id"], "original")
        assert "/api/assets/" in asset["urls"]["file"]
        if a["image_id"] is not None:
            img = get_image(a["image_id"])
            assert img["data_base64"]
        # resolve_url surfaces alice's own file.
        hits = resolve_url("https://drive.example/alice-doc")
        assert [h.id for h in hits] == [a["file_id"]]


# ---------------------------------------------------------------------------
# Explicit grant restores cross-account access through the tools.
# ---------------------------------------------------------------------------


def test_grant_makes_foreign_file_reachable(env: None, tmp_path: Path) -> None:
    t = _two_tenants(tmp_path)
    b = t["b"]
    with session_scope() as s:
        alice = s.execute(select(User).where(User.email == "alice@x")).scalar_one()
        s.add(FolderAcl(folder_id=b["folder_id"], user_id=alice.id))

    with _as_account("alice@x"):
        assert "beta" in get_file(b["file_id"])["text"]
        if b["image_id"] is not None:
            assert get_image(b["image_id"])["data_base64"]


# ---------------------------------------------------------------------------
# No caller context (single-user / in-process) keeps full access — the
# desktop deployment must not regress.
# ---------------------------------------------------------------------------


def test_no_context_sees_everything(env: None, tmp_path: Path) -> None:
    t = _two_tenants(tmp_path)
    # No _as_account: _resolved_user_id() is None → gate is a no-op.
    assert "alpha" in get_file(t["a"]["file_id"])["text"]
    assert "beta" in get_file(t["b"]["file_id"])["text"]
