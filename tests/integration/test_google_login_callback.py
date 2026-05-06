"""Integration test: Google sign-in callback enforces the allowlist.

Goes through the real FastAPI route. The Google token + userinfo HTTP
calls are stubbed by replacing the ``httpx`` module the route imports;
no network traffic. We assert the gate's behaviour, not OAuth internals.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _StubResponse:
    def __init__(self, status_code: int, body: dict[str, object]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = ""

    def json(self) -> dict[str, object]:
        return self._body


class _StubAsyncClient:
    """Replacement for ``httpx.AsyncClient``. Returns a fixed token then
    a userinfo payload supplied by the test (via the module-level dict)."""

    def __init__(self, *_: object, **__: object) -> None:
        pass

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, *_: object, **__: object) -> _StubResponse:
        return _StubResponse(200, {"access_token": "stub-token"})

    async def get(self, *_: object, **__: object) -> _StubResponse:
        return _StubResponse(200, _USERINFO)


_USERINFO: dict[str, object] = {}


@pytest.fixture
def stubbed_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.api.routes import auth as auth_mod

    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", _StubAsyncClient)


@pytest.fixture
def google_app(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[FastAPI]:
    """App with Google OAuth configured and the allowlist driven by tfvars-
    style env. The caller mutates ``VOITTA_ALLOWED_DOMAINS`` /
    ``VOITTA_USERS_FILE`` per-test before instantiating the client.
    """
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.main import create_app

    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", "stub-client-id")
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", "stub-client-secret")
    monkeypatch.setenv("VOITTA_SESSION_SECRET", "x" * 64)
    monkeypatch.delenv("VOITTA_DEV_USER", raising=False)
    monkeypatch.delenv("VOITTA_SINGLE_USER", raising=False)
    reset_settings_cache()
    yield create_app()


def _login(
    app: FastAPI,
    email: str,
    *,
    email_verified: bool = True,
) -> tuple[int, str]:
    """Drive the OAuth callback end-to-end. Returns (status_code, body)."""
    global _USERINFO
    _USERINFO = {"email": email, "email_verified": email_verified, "name": "T"}
    with TestClient(app) as c:
        # Prime the session with the state the callback expects.
        start = c.get("/api/auth/login/google", follow_redirects=False)
        assert start.status_code == 307
        # Pull the state from the redirect URL.
        loc = start.headers["location"]
        state = loc.split("state=", 1)[1].split("&", 1)[0]
        resp = c.get(
            f"/api/auth/google/callback?code=stub&state={state}",
            follow_redirects=False,
        )
        return resp.status_code, resp.text


def test_login_allowed_when_domain_matches(
    google_app: FastAPI,
    stubbed_httpx: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_ALLOWED_DOMAINS", "customer.com")
    reset_settings_cache()
    app = google_app
    code, _ = _login(app, "alice@customer.com")
    assert code == 303  # redirect home


def test_login_allowed_when_email_in_users_file(
    google_app: FastAPI,
    stubbed_httpx: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    extras = tmp_path / "extras.txt"
    extras.write_text("bob@gmail.com\n")
    monkeypatch.setenv("VOITTA_USERS_FILE", str(extras))
    reset_settings_cache()
    app = google_app
    code, _ = _login(app, "bob@gmail.com")
    assert code == 303


def test_login_denied_when_neither_matches(
    google_app: FastAPI,
    stubbed_httpx: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_ALLOWED_DOMAINS", "customer.com")
    reset_settings_cache()
    app = google_app
    code, body = _login(app, "eve@evil.example")
    assert code == 403
    assert "not authorized" in body.lower()


def test_login_denied_when_no_allowlist_configured(
    google_app: FastAPI,
    stubbed_httpx: None,
) -> None:
    """Fail-loud default: with no domains and no users.txt, every sign-in is
    rejected — no silent accept-all on a misconfigured deploy.
    """
    app = google_app
    code, _ = _login(app, "alice@customer.com")
    assert code == 403
