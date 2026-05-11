"""Integration tests for the on-demand asset framework.

Covers the channel itself — token signing, ACL gating, handler
dispatch, ``list_assets`` reading the parser-supplied menu — using a
synthetic handler so the tests don't depend on OSMesa being installed
locally. The CAD-specific render path is exercised at deploy time
against the live server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.cas import store as cas_store
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder
from voitta_rag_enterprise.services import asset_handlers, signed_assets
from voitta_rag_enterprise.services.asset_handlers import (
    AssetHandler,
    AssetResponse,
    AssetSpec,
    RenderedAsset,
)

from ..conftest import auth_as


class _EchoHandler(AssetHandler):
    """Test handler that echoes its inputs as bytes.

    Lets us exercise the token round-trip and the route without
    pulling in a real renderer. Each variant produces a different
    body so the variant-routing branch is testable."""

    asset_type = "test_echo"

    def validate_params(self, params):
        # Tiny validation: ``size`` must be a positive int if present.
        out = dict(params or {})
        if "size" in out:
            try:
                v = int(out["size"])
            except (TypeError, ValueError) as e:
                raise ValueError("size: not an integer") from e
            if v < 1:
                raise ValueError("size: must be positive")
            out["size"] = v
        return out

    def request(self, *, file_id, slug, params, user_id):
        variants = params.pop("variants", ["a", "b"])
        urls = {}
        expires_at = 0
        for v in variants:
            token, exp = signed_assets.issue_token(
                file_id=file_id,
                asset_type=self.asset_type,
                slug=slug,
                params={**params, "__variant__": v},
                user_id=user_id,
            )
            urls[v] = f"/api/assets/{token}"
            expires_at = exp
        return AssetResponse(asset_type=self.asset_type, urls=urls, expires_at=expires_at)

    def fetch(self, *, file_id, slug, params, user_id, variant):
        body = f"file={file_id};slug={slug};variant={variant};params={params}".encode()
        return RenderedAsset(body=body, mime="text/plain")


@pytest.fixture(autouse=True)
def _register_echo_handler():
    """Register the echo handler for every test in this module.

    The framework's registry is process-wide, so we re-register on
    each test (registration is idempotent for the same handler
    instance)."""
    asset_handlers.register(_EchoHandler())
    yield


def _seed_inside_client(c: TestClient, app: FastAPI, email: str = "tester@x.com") -> int:
    """Create a folder + file row using the live TestClient.

    The MCP transport's session manager is single-use per app
    instance, so every test must do all its HTTP work in the same
    ``with TestClient(app)`` block."""
    auth_as(app, email)
    r = c.post("/api/folders", json={"name": "folder"})
    folder_id = r.json()["id"]
    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        f = File(
            folder_id=folder.id,
            rel_path="fixture.bin",
            size_bytes=11,
            mtime_ns=0,
            last_seen_at=0,
            state="indexed",
        )
        s.add(f)
        s.flush()
        return f.id


def test_round_trip_through_route_returns_bytes(app: FastAPI) -> None:
    """Mint a token via the handler, fetch via the route, get the
    echo-handler's body back. End-to-end shake of the channel."""
    with TestClient(app) as c:
        file_id = _seed_inside_client(c, app)
        with session_scope() as s:
            from voitta_rag_enterprise.db.models import User
            uid = s.execute(select(User)).scalar_one().id
        handler = asset_handlers.get_handler("test_echo")
        resp = handler.request(
            file_id=file_id,
            slug="alpha",
            params={"size": 320},
            user_id=uid,
        )
        assert resp.urls
        for variant, url in resp.urls.items():
            r = c.get(url)
            assert r.status_code == 200, r.text
            body = r.content.decode()
            assert f"file={file_id}" in body
            assert "slug=alpha" in body
            assert f"variant={variant}" in body


def test_route_rejects_tampered_token(app: FastAPI) -> None:
    with TestClient(app) as c:
        _seed_inside_client(c, app)
        r = c.get("/api/assets/garbage.token")
        assert r.status_code == 401


def test_route_rejects_token_for_other_user(app: FastAPI) -> None:
    """Tokens carry the issuing user's id. Re-presenting a token as a
    different signed-in user must be rejected."""
    with TestClient(app) as c:
        file_id = _seed_inside_client(c, app, email="alice@x.com")
        with session_scope() as s:
            from voitta_rag_enterprise.db.models import User
            alice_id = s.execute(select(User).where(User.email == "alice@x.com")).scalar_one().id
        handler = asset_handlers.get_handler("test_echo")
        resp = handler.request(
            file_id=file_id, slug=None, params={"variants": ["a"]}, user_id=alice_id,
        )
        bob_url = list(resp.urls.values())[0]
        auth_as(app, "bob@x.com")
        r = c.get(bob_url)
        assert r.status_code == 401


def test_route_returns_400_on_unknown_asset_type(app: FastAPI) -> None:
    """A token referencing an unregistered asset_type is still
    well-formed; the route validates the type at dispatch time."""
    with TestClient(app) as c:
        file_id = _seed_inside_client(c, app)
        with session_scope() as s:
            from voitta_rag_enterprise.db.models import User
            uid = s.execute(select(User)).scalar_one().id
        token, _ = signed_assets.issue_token(
            file_id=file_id,
            asset_type="does_not_exist",
            slug=None,
            params={},
            user_id=uid,
        )
        r = c.get(f"/api/assets/{token}")
        assert r.status_code == 400
        assert "does_not_exist" in r.text


def test_list_assets_reads_parser_menu(env: None, tmp_path: Path) -> None:
    """The MCP ``list_assets`` tool reads from
    ``on_demand_assets.json`` in the file's CAS dir. Write one
    directly and verify it round-trips."""
    init_db()
    folder_path = tmp_path / "folder"
    folder_path.mkdir()
    with session_scope() as s:
        folder = Folder(path=str(folder_path), display_name="folder")
        s.add(folder)
        s.flush()
        f = File(
            folder_id=folder.id, rel_path="fixture.glb",
            size_bytes=10, mtime_ns=0, last_seen_at=0, state="indexed",
            file_cas_id="testsha000000000000000000000000",
        )
        s.add(f)
        s.flush()
        file_id = f.id
        cas_id = f.file_cas_id

    import json as _json
    cas_store.write_file_blob(
        cas_id,
        "on_demand_assets.json",
        _json.dumps([
            {
                "asset_type": "cad_projection",
                "label": "Render Foo",
                "description": "...",
                "slug": "foo",
                "params_schema": {"type": "object"},
                "examples": [{"size": 320}],
            }
        ]),
    )

    from voitta_rag_enterprise.mcp_server import list_assets

    out = list_assets(file_id)
    assert len(out) == 1
    assert out[0]["asset_type"] == "cad_projection"
    assert out[0]["slug"] == "foo"


def test_request_asset_invokes_handler_and_returns_urls(
    env: None, tmp_path: Path,
) -> None:
    """End-to-end: the request_asset MCP tool resolves a handler and
    returns its AssetResponse as a plain dict."""
    init_db()
    folder_path = tmp_path / "folder"
    folder_path.mkdir()
    with session_scope() as s:
        folder = Folder(path=str(folder_path), display_name="folder")
        s.add(folder)
        s.flush()
        f = File(
            folder_id=folder.id, rel_path="x.bin",
            size_bytes=1, mtime_ns=0, last_seen_at=0, state="indexed",
        )
        s.add(f)
        s.flush()
        file_id = f.id

    from voitta_rag_enterprise.mcp_server import request_asset

    out = request_asset(
        file_id=file_id,
        asset_type="test_echo",
        slug="alpha",
        params={"variants": ["x"]},
    )
    assert out["asset_type"] == "test_echo"
    assert "urls" in out and "x" in out["urls"]
    assert out["urls"]["x"].startswith("/api/assets/")
    assert "expires_at" in out


def test_request_asset_rejects_unknown_asset_type(env: None) -> None:
    from voitta_rag_enterprise.mcp_server import request_asset
    init_db()
    with pytest.raises(ValueError, match="unknown asset_type"):
        request_asset(file_id=1, asset_type="nope", slug=None, params=None)
