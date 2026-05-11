"""HMAC-signed asset URLs.

Issues short-lived tokens that name a derived artifact to render. The
LLM gets the token packaged as ``/api/assets/{token}`` from
``request_asset`` and follows that URL to fetch the bytes.

Token shape (compact JSON, base64url-encoded, dot-separated signature):

    payload_b64.signature_b64

where payload is ``{fid, type, slug, params, exp, uid}`` and signature
is ``HMAC-SHA256(secret, payload_b64)``.

Verification is constant-time. Expired or tampered tokens come back as
:exc:`InvalidAssetToken` with no further detail (don't leak which check
failed).

No claim is made about idempotency or replay protection beyond expiry —
a still-valid token can be fetched multiple times. That's fine: renders
are deterministic, the handlers do their own ACL gate, and we
deliberately avoid caching (see ``cad_render``: every call re-renders).

The signing key is derived from :attr:`Settings.session_secret` unless
``VOITTA_ASSET_TOKEN_SECRET`` is set explicitly. TTL defaults to one
hour; override with ``VOITTA_ASSET_TOKEN_TTL_SECONDS``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from ..config import get_settings


class InvalidAssetToken(Exception):
    """Token failed signature, decode, or expiry check."""


@dataclass(frozen=True)
class AssetClaims:
    """Decoded token payload — everything needed to dispatch to a handler.

    ``params`` is whatever JSON-shaped dict the caller of
    :func:`issue_token` passed in; handlers re-validate it against their
    own params schema before doing real work."""

    file_id: int
    asset_type: str
    slug: str | None
    params: dict[str, Any]
    user_id: int | None
    expires_at: int

    @property
    def expired(self) -> bool:
        return self.expires_at < int(time.time())


def _secret() -> bytes:
    """Resolve the HMAC key. Prefers the dedicated env var; falls back
    to the resolved session secret (which itself persists a random
    value to disk on first call) so a deploy with no explicit asset
    secret still gets a stable key across restarts."""
    explicit = os.environ.get("VOITTA_ASSET_TOKEN_SECRET")
    if explicit:
        return explicit.encode("utf-8")
    sess = get_settings().resolved_session_secret()
    return hashlib.sha256(sess.encode("utf-8") + b"|asset_tokens").digest()


def _ttl_seconds() -> int:
    raw = os.environ.get("VOITTA_ASSET_TOKEN_TTL_SECONDS")
    if raw is None:
        return 3600
    try:
        v = int(raw)
    except ValueError:
        return 3600
    # Bound: tokens shorter than 30s are useless; longer than a day
    # defeat the "short-lived" intent.
    return max(30, min(v, 86400))


def _b64encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    # urlsafe + padding restore — Python is strict about ``=`` padding.
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(
    *,
    file_id: int,
    asset_type: str,
    slug: str | None,
    params: dict[str, Any] | None,
    user_id: int | None,
    ttl_seconds: int | None = None,
) -> tuple[str, int]:
    """Mint a signed token and return ``(token, expires_at)``.

    ``params`` is serialized into the token verbatim. Keep it small —
    each render request re-validates against the handler's schema, so
    the token is just an authenticated parameter envelope, not the
    source of truth for what the handler ultimately accepts.
    """
    if ttl_seconds is None:
        ttl_seconds = _ttl_seconds()
    expires_at = int(time.time()) + ttl_seconds
    payload = {
        "fid": file_id,
        "type": asset_type,
        "slug": slug,
        "p": params or {},
        "uid": user_id,
        "exp": expires_at,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    sig = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = _b64encode(sig)
    return f"{payload_b64}.{sig_b64}", expires_at


def verify_token(token: str) -> AssetClaims:
    """Validate signature + expiry; return the decoded claims.

    Raises :class:`InvalidAssetToken` on any failure — signature,
    decode, or past-expiry. The error message is deliberately generic
    so callers can't probe the token state by varying inputs.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError as e:
        raise InvalidAssetToken("malformed token") from e
    try:
        expected_sig = hmac.new(
            _secret(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        actual_sig = _b64decode(sig_b64)
    except (ValueError, TypeError) as e:
        raise InvalidAssetToken("malformed token") from e
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise InvalidAssetToken("bad signature")
    try:
        payload = json.loads(_b64decode(payload_b64))
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise InvalidAssetToken("malformed payload") from e
    claims = AssetClaims(
        file_id=int(payload["fid"]),
        asset_type=str(payload["type"]),
        slug=payload.get("slug"),
        params=dict(payload.get("p") or {}),
        user_id=payload.get("uid"),
        expires_at=int(payload["exp"]),
    )
    if claims.expired:
        raise InvalidAssetToken("token expired")
    return claims
