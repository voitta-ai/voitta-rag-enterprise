"""Sign / verify roundtrip + tamper / expiry / unknown-key cases for
the signed-asset token plumbing."""

from __future__ import annotations

import os
import time

import pytest

from voitta_rag_enterprise.services import signed_assets


def _set_secret(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    """Pin the HMAC key via the dedicated env var so tests are
    independent of any session_secret state the suite may carry."""
    monkeypatch.setenv("VOITTA_ASSET_TOKEN_SECRET", secret)


def test_roundtrip_preserves_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    token, exp = signed_assets.issue_token(
        file_id=42,
        asset_type="cad_projection",
        slug="floor-pulley-rr-a",
        params={"size": 320, "__variant__": "iso"},
        user_id=7,
    )
    assert exp > int(time.time())
    claims = signed_assets.verify_token(token)
    assert claims.file_id == 42
    assert claims.asset_type == "cad_projection"
    assert claims.slug == "floor-pulley-rr-a"
    assert claims.params == {"size": 320, "__variant__": "iso"}
    assert claims.user_id == 7
    assert claims.expires_at == exp
    assert not claims.expired


def test_tampered_payload_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    token, _ = signed_assets.issue_token(
        file_id=1, asset_type="x", slug=None, params=None, user_id=None
    )
    payload_b64, sig_b64 = token.split(".", 1)
    # Flip a byte in the payload — same length, different content.
    bad_payload = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
    bad_token = f"{bad_payload}.{sig_b64}"
    with pytest.raises(signed_assets.InvalidAssetToken):
        signed_assets.verify_token(bad_token)


def test_tampered_signature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    token, _ = signed_assets.issue_token(
        file_id=1, asset_type="x", slug=None, params=None, user_id=None
    )
    payload_b64, sig_b64 = token.split(".", 1)
    bad_sig = sig_b64[:-1] + ("Z" if sig_b64[-1] != "Z" else "Y")
    with pytest.raises(signed_assets.InvalidAssetToken):
        signed_assets.verify_token(f"{payload_b64}.{bad_sig}")


def test_different_secret_rejects_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "key-A")
    token, _ = signed_assets.issue_token(
        file_id=1, asset_type="x", slug=None, params=None, user_id=None
    )
    _set_secret(monkeypatch, "key-B")
    with pytest.raises(signed_assets.InvalidAssetToken):
        signed_assets.verify_token(token)


def test_expired_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    token, _ = signed_assets.issue_token(
        file_id=1,
        asset_type="x",
        slug=None,
        params=None,
        user_id=None,
        ttl_seconds=60,  # any positive value; we monkey-patch time below
    )
    # Fast-forward — the verify path checks against time.time() directly.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 9999)
    with pytest.raises(signed_assets.InvalidAssetToken):
        signed_assets.verify_token(token)


def test_malformed_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    with pytest.raises(signed_assets.InvalidAssetToken):
        signed_assets.verify_token("not-a-real-token")
    with pytest.raises(signed_assets.InvalidAssetToken):
        signed_assets.verify_token("missing.dot")
    with pytest.raises(signed_assets.InvalidAssetToken):
        signed_assets.verify_token("aGVsbG8.dGVzdA")  # decodes but bad JSON


def test_ttl_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    monkeypatch.delenv("VOITTA_ASSET_TOKEN_TTL_SECONDS", raising=False)
    _, exp = signed_assets.issue_token(
        file_id=1, asset_type="x", slug=None, params=None, user_id=None
    )
    # Default is 3600s; allow generous slack for the time.time() race.
    now = int(time.time())
    assert 3590 <= exp - now <= 3700


def test_ttl_clamped_to_safe_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    monkeypatch.setenv("VOITTA_ASSET_TOKEN_TTL_SECONDS", "1")
    _, exp = signed_assets.issue_token(
        file_id=1, asset_type="x", slug=None, params=None, user_id=None
    )
    now = int(time.time())
    # Floor is 30 seconds.
    assert exp - now >= 29


def test_explicit_ttl_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret(monkeypatch, "test-key")
    monkeypatch.setenv("VOITTA_ASSET_TOKEN_TTL_SECONDS", "300")
    _, exp = signed_assets.issue_token(
        file_id=1,
        asset_type="x",
        slug=None,
        params=None,
        user_id=None,
        ttl_seconds=60,
    )
    now = int(time.time())
    assert 55 <= exp - now <= 65


def test_falls_back_to_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the dedicated asset-token secret isn't set, derivation
    falls back to session_secret so a deploy without explicit
    configuration still gets a stable signing key."""
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.delenv("VOITTA_ASSET_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("VOITTA_SESSION_SECRET", "from-session")
    reset_settings_cache()
    token, _ = signed_assets.issue_token(
        file_id=99, asset_type="t", slug=None, params=None, user_id=None
    )
    claims = signed_assets.verify_token(token)
    assert claims.file_id == 99
