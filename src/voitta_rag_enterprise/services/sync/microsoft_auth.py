"""Microsoft Graph auth + thin HTTP helpers.

Shared by :mod:`services.sync.sharepoint` and :mod:`services.sync.teams`.
Mirrors the OAuth / token / retry surface that
:mod:`services.sync.google_drive` exposes for Google.

Three auth modes:

* ``oauth`` — delegated (user-consented) flow. Refresh token stored on
  the source row; access tokens are minted on demand and never persisted.
  Microsoft rotates the refresh token on most refresh calls — callers
  must persist the new one when present.
* ``app_secret`` — client-credentials with a client_secret. Tenant-wide
  application permissions (``Sites.Read.All``, ``Files.Read.All``, …),
  requires admin consent.
* ``app_cert`` — client-credentials with a PEM private key (RSA). We
  build a self-signed JWT assertion and exchange it for a bearer token.
  Same effective permissions as app_secret, just stricter on creds.

Scope-check
-----------
Tokens carry their granted scopes in the JWT body — ``scp`` (space-
separated) for delegated tokens, ``roles`` (array) for app-only. The
config page calls :func:`compute_missing_scopes` to surface "this
connection is missing X — feature Y will be skipped" warnings.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoints + scopes
# ---------------------------------------------------------------------------

MS_LOGIN_BASE = "https://login.microsoftonline.com"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Delegated scopes we ask for at consent time. We ask for everything
# both connectors might need so the user only sees one consent screen.
# A missing scope at runtime degrades the affected feature; it doesn't
# abort the sync.
DELEGATED_SCOPES = (
    "offline_access "
    "Sites.Read.All "
    "Files.Read.All "
    "Notes.Read.All "
    "User.Read.All "
    "OnlineMeetings.Read "
    "OnlineMeetingTranscript.Read.All "
    "CallRecords.Read.All"
)

# Application (app-only) permissions are not requested per-call — they're
# whatever the tenant admin granted on the app registration. We list the
# ones we recognise here only to drive the scope-check UI.
APP_PERMISSIONS = (
    "Sites.Read.All",
    "Files.Read.All",
    "Notes.Read.All",
    "User.Read.All",
    "OnlineMeetings.Read.All",
    "OnlineMeetingTranscript.Read.All",
    "CallRecords.Read.All",
)


# Per-feature scope requirements. Drives the scope-check endpoint: if a
# feature's required delegated/app scope isn't in the token, surface the
# ``impact`` string.
FEATURE_SCOPES: list[dict[str, str]] = [
    {
        "feature": "SharePoint files",
        "delegated": "Sites.Read.All",
        "app": "Sites.Read.All",
        "impact": "Site files cannot be listed or downloaded.",
    },
    {
        "feature": "OneDrive / drive items",
        "delegated": "Files.Read.All",
        "app": "Files.Read.All",
        "impact": "Drive content download will fail.",
    },
    {
        "feature": "OneNote notebooks",
        "delegated": "Notes.Read.All",
        "app": "Notes.Read.All",
        "impact": "OneNote pages will be skipped.",
    },
    {
        "feature": "Tenant user picker",
        "delegated": "User.Read.All",
        "app": "User.Read.All",
        "impact": "The 'specific user' Teams picker cannot list users.",
    },
    {
        "feature": "Teams meetings (organized)",
        "delegated": "OnlineMeetings.Read",
        "app": "OnlineMeetings.Read.All",
        "impact": "Meetings the user organized cannot be enumerated.",
    },
    {
        "feature": "Teams transcripts",
        "delegated": "OnlineMeetingTranscript.Read.All",
        "app": "OnlineMeetingTranscript.Read.All",
        "impact": "Meeting transcripts will be skipped.",
    },
    {
        "feature": "Teams meetings (attended via call records)",
        "delegated": "CallRecords.Read.All",
        "app": "CallRecords.Read.All",
        "impact": (
            "Meetings the user only attended (did not organize) will be "
            "missed. Admin consent is required for this scope."
        ),
    },
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MicrosoftAuth:
    """Snapshot of credentials taken before releasing the DB session.

    Connectors take a copy of these fields at sync-job entry so the
    (slow) network work can run without holding the SQLAlchemy session.
    """

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    cert_pem: str = ""
    refresh_token: str = ""
    method: str = ""  # "oauth" | "app_secret" | "app_cert"
    # When refresh rotation produces a new refresh_token, the connector
    # writes it here so the API layer can persist it back to the DB.
    rotated_refresh_token: str | None = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        if not (self.tenant_id and self.client_id):
            return False
        if self.method == "oauth":
            return bool(self.client_secret and self.refresh_token)
        if self.method == "app_secret":
            return bool(self.client_secret)
        if self.method == "app_cert":
            return bool(self.cert_pem)
        return False


# ---------------------------------------------------------------------------
# OAuth URL + token exchange
# ---------------------------------------------------------------------------


def get_auth_url(
    tenant_id: str, client_id: str, redirect_uri: str, state: str
) -> str:
    """Build the Microsoft OAuth2 authorization URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": DELEGATED_SCOPES,
        "state": state,
        # ``prompt=consent`` ensures a refresh_token is re-issued even
        # when this client has been consented to before (otherwise we
        # only get one on first consent — same problem Google has).
        "prompt": "consent",
    }
    return (
        f"{MS_LOGIN_BASE}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(params)}"
    )


async def exchange_code_for_tokens(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    url = f"{MS_LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "scope": DELEGATED_SCOPES,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(_token_error("exchange", resp))
    return resp.json()


async def refresh_access_token(auth: MicrosoftAuth) -> str:
    """Mint an access token from the delegated refresh_token.

    Rotates ``auth.rotated_refresh_token`` when Microsoft hands back a
    new refresh_token (which is most of the time). Callers must check
    that field after sync and persist the new value.
    """
    if auth.method != "oauth":
        raise RuntimeError(
            f"refresh_access_token called with method={auth.method!r}; "
            "use get_app_only_token for app-only auth"
        )
    if not auth.refresh_token:
        raise RuntimeError(
            "Microsoft sync not connected. Click 'Connect' to sign in."
        )

    url = f"{MS_LOGIN_BASE}/{auth.tenant_id}/oauth2/v2.0/token"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            data={
                "grant_type": "refresh_token",
                "client_id": auth.client_id,
                "client_secret": auth.client_secret,
                "refresh_token": auth.refresh_token,
                "scope": DELEGATED_SCOPES,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(_token_error("refresh", resp))
    body = resp.json()
    new_rt = body.get("refresh_token")
    if new_rt and new_rt != auth.refresh_token:
        auth.rotated_refresh_token = new_rt
        logger.info("Microsoft refresh token rotated for tenant=%s", auth.tenant_id)
    return body["access_token"]


# ---------------------------------------------------------------------------
# App-only (client credentials)
# ---------------------------------------------------------------------------


async def get_app_only_token(auth: MicrosoftAuth) -> str:
    """Mint a tenant-wide app-only token via client_credentials."""
    if auth.method not in ("app_secret", "app_cert"):
        raise RuntimeError(
            f"get_app_only_token called with method={auth.method!r}"
        )

    url = f"{MS_LOGIN_BASE}/{auth.tenant_id}/oauth2/v2.0/token"
    data: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": auth.client_id,
        "scope": "https://graph.microsoft.com/.default",
    }
    if auth.method == "app_secret":
        data["client_secret"] = auth.client_secret
    else:
        assertion = _build_client_assertion(
            tenant_id=auth.tenant_id,
            client_id=auth.client_id,
            cert_pem=auth.cert_pem,
        )
        data["client_assertion_type"] = (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        data["client_assertion"] = assertion

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=data)
    if resp.status_code != 200:
        raise RuntimeError(_token_error("app-only", resp))
    return resp.json()["access_token"]


def _build_client_assertion(
    *, tenant_id: str, client_id: str, cert_pem: str
) -> str:
    """Build the JWT client assertion for cert-based app auth.

    ``cert_pem`` must contain both an ``-----BEGIN PRIVATE KEY-----``
    block and an ``-----BEGIN CERTIFICATE-----`` block (the public cert
    is used to compute the SHA-1 thumbprint Azure AD requires in the
    JWT header).
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as e:
        raise RuntimeError(
            "Certificate-based Microsoft auth requires the 'cryptography' "
            "package. pip install cryptography."
        ) from e

    pem_bytes = cert_pem.encode("utf-8")
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    cert = x509.load_pem_x509_certificate(pem_bytes)
    thumbprint = cert.fingerprint(hashes.SHA1())
    x5t = base64.urlsafe_b64encode(thumbprint).rstrip(b"=").decode("ascii")

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "x5t": x5t}
    payload = {
        "aud": f"{MS_LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token",
        "iss": client_id,
        "sub": client_id,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "exp": now + 600,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + b"."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return (signing_input + b"." + _b64url(signature)).decode("ascii")


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


# ---------------------------------------------------------------------------
# Token-scope decoder
# ---------------------------------------------------------------------------


def decode_token_scopes(access_token: str) -> dict[str, list[str]]:
    """Return ``{"scp": [...], "roles": [...]}`` from the token JWT body.

    Microsoft access tokens are unencrypted JWTs (header.body.signature
    base64url-encoded). We do not verify the signature — we only need to
    read the granted-permissions claims to drive the config-page warning
    panel. Returns empty lists on any decode failure.
    """
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return {"scp": [], "roles": []}
        body_raw = parts[1] + "=" * (-len(parts[1]) % 4)
        body = json.loads(base64.urlsafe_b64decode(body_raw))
    except Exception:  # noqa: BLE001 — best effort, swallow + return empty
        return {"scp": [], "roles": []}
    scp = body.get("scp") or ""
    roles = body.get("roles") or []
    return {
        "scp": scp.split() if isinstance(scp, str) else [],
        "roles": list(roles) if isinstance(roles, list) else [],
    }


def compute_missing_scopes(
    access_token: str, *, app_only: bool
) -> dict[str, list[dict[str, str]]]:
    """Compare the token's granted scopes against ``FEATURE_SCOPES``.

    Returns ``{"granted": [...], "missing": [{feature, scope, impact}, ...]}``.
    ``app_only`` selects which scope column we consult (``roles`` claim
    vs ``scp`` claim).
    """
    decoded = decode_token_scopes(access_token)
    granted_set: set[str] = set(decoded["roles"] if app_only else decoded["scp"])
    key = "app" if app_only else "delegated"

    missing: list[dict[str, str]] = []
    for entry in FEATURE_SCOPES:
        required = entry[key]
        if required not in granted_set:
            missing.append(
                {
                    "feature": entry["feature"],
                    "scope": required,
                    "impact": entry["impact"],
                }
            )
    return {"granted": sorted(granted_set), "missing": missing}


# ---------------------------------------------------------------------------
# Thin HTTP helpers with 429 backoff
# ---------------------------------------------------------------------------


async def graph_get(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    max_retries: int = 4,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET ``url`` with bearer auth + 429 backoff (respects Retry-After)."""
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(max_retries + 1):
        resp = await client.get(url, headers=headers)
        if resp.status_code != 429:
            return resp
        delay = _retry_after(resp, attempt)
        logger.warning(
            "Graph 429, retry %d/%d in %ds: %s",
            attempt + 1, max_retries, delay, url[:120],
        )
        await asyncio.sleep(delay)
    return resp  # type: ignore[return-value]


async def graph_post(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    json_body: dict | None = None,
    *,
    max_retries: int = 4,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(max_retries + 1):
        resp = await client.post(url, headers=headers, json=json_body)
        if resp.status_code != 429:
            return resp
        delay = _retry_after(resp, attempt)
        logger.warning(
            "Graph 429 POST, retry %d/%d in %ds: %s",
            attempt + 1, max_retries, delay, url[:120],
        )
        await asyncio.sleep(delay)
    return resp  # type: ignore[return-value]


def _retry_after(resp: httpx.Response, attempt: int) -> int:
    try:
        delay = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
    except ValueError:
        delay = 2 ** (attempt + 1)
    return min(delay, 30)


def extract_graph_error(resp: httpx.Response) -> str:
    """Human-readable error string from a Graph error response body."""
    try:
        body = resp.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        code = error.get("code", "")
        message = error.get("message", "")
        if code or message:
            return f"{code}: {message}" if code else message
    except Exception:  # noqa: BLE001
        pass
    return resp.text[:500] if resp.text else f"HTTP {resp.status_code}"


def raise_graph_error(resp: httpx.Response, context: str) -> None:
    detail = extract_graph_error(resp)
    hint = ""
    if resp.status_code == 401:
        hint = " (Token may be expired — try reconnecting)"
    elif resp.status_code == 403:
        hint = " (Access denied — required scope may not be granted)"
    raise RuntimeError(
        f"Microsoft Graph {context} failed ({resp.status_code}): {detail}{hint}"
    )


def _token_error(action: str, resp: httpx.Response) -> str:
    try:
        body = resp.json()
        desc = body.get("error_description") or body.get("error") or ""
    except Exception:  # noqa: BLE001
        desc = resp.text[:500]
    return f"Microsoft token {action} failed ({resp.status_code}): {desc}"
