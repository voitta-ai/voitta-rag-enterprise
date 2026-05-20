"""Unit tests for Microsoft auth helpers — pure-function bits only.

Network-bound paths (OAuth exchange, app-only token, graph_get) need
their own integration coverage; here we lock down the token decoder +
scope-check shape so the config-page warning panel stays accurate.
"""

from __future__ import annotations

import base64
import json

from voitta_rag_enterprise.services.sync.microsoft_auth import (
    FEATURE_SCOPES,
    MicrosoftAuth,
    compute_missing_scopes,
    decode_token_scopes,
)


def _make_token(claims: dict) -> str:
    """Build a fake unsigned JWT with the given body claims."""

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64url(b'{"alg":"none","typ":"JWT"}')
    body = b64url(json.dumps(claims).encode("utf-8"))
    return f"{header}.{body}."


def test_decode_token_scopes_delegated():
    token = _make_token({"scp": "Sites.Read.All Files.Read.All Notes.Read.All"})
    decoded = decode_token_scopes(token)
    assert "Sites.Read.All" in decoded["scp"]
    assert "Files.Read.All" in decoded["scp"]
    assert decoded["roles"] == []


def test_decode_token_scopes_app_only():
    token = _make_token({"roles": ["Sites.Read.All", "Files.Read.All"]})
    decoded = decode_token_scopes(token)
    assert decoded["roles"] == ["Sites.Read.All", "Files.Read.All"]
    assert decoded["scp"] == []


def test_decode_token_scopes_garbage():
    assert decode_token_scopes("not.a.real.jwt") == {"scp": [], "roles": []}
    assert decode_token_scopes("") == {"scp": [], "roles": []}


def test_compute_missing_scopes_full_grant_delegated():
    granted = " ".join(s["delegated"] for s in FEATURE_SCOPES)
    token = _make_token({"scp": granted})
    report = compute_missing_scopes(token, app_only=False)
    assert report["missing"] == []
    for s in FEATURE_SCOPES:
        assert s["delegated"] in report["granted"]


def test_compute_missing_scopes_partial_grant_app():
    token = _make_token({"roles": ["Sites.Read.All"]})
    report = compute_missing_scopes(token, app_only=True)
    assert report["granted"] == ["Sites.Read.All"]
    missing_scopes = {m["scope"] for m in report["missing"]}
    # Anything that isn't Sites.Read.All should be missing.
    assert "OnlineMeetingTranscript.Read.All" in missing_scopes
    # Every missing entry carries impact text.
    for m in report["missing"]:
        assert m["impact"]
        assert m["feature"]


def test_microsoft_auth_configured_oauth():
    auth = MicrosoftAuth(
        tenant_id="t", client_id="c", client_secret="s",
        refresh_token="r", method="oauth",
    )
    assert auth.configured is True


def test_microsoft_auth_configured_app_secret():
    auth = MicrosoftAuth(
        tenant_id="t", client_id="c", client_secret="s", method="app_secret",
    )
    assert auth.configured is True


def test_microsoft_auth_not_configured_app_cert_without_pem():
    auth = MicrosoftAuth(
        tenant_id="t", client_id="c", method="app_cert",
    )
    assert auth.configured is False


def test_microsoft_auth_not_configured_oauth_without_refresh():
    auth = MicrosoftAuth(
        tenant_id="t", client_id="c", client_secret="s", method="oauth",
    )
    assert auth.configured is False
