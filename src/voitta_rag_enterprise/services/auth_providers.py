"""Auth-providers list — admin-managed OAuth credentials catalog.

This module is intentionally a *list*. It does not drive the login flow
(``api/routes/auth.py`` still reads ``Settings.google_auth_*``). The
admin UI uses it to track every (provider, client_id, client_secret)
triple the deployment knows about so a future expansion can switch
providers without redeploying.

Two responsibilities:

1. **Bootstrap** — :func:`upsert_env_provider` is called from the app
   lifespan. It looks for an ``auth_providers`` row with
   ``provider='google'``, ``client_id == VOITTA_GOOGLE_AUTH_CLIENT_ID``;
   if missing, inserts one tagged ``source='env'``. Deleting the row in
   the UI re-creates it on the next restart while the env vars are
   still set — that's how "what is in .env should always be in the
   list" works.

2. **Validity check** — :func:`check_provider` answers "are these
   credentials accepted by the provider?" by abusing the OAuth token
   endpoint: POST a bogus authorization code; the error code
   distinguishes credential failures from grant failures. Implemented
   for Google today; Microsoft/GitHub return ``not_implemented``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AuthProvider

logger = logging.getLogger(__name__)


# Provider-type → human-readable default label. Used when the admin
# leaves the ``label`` field blank on create.
PROVIDER_LABELS: dict[str, str] = {
    "google": "Google",
    "microsoft": "Microsoft",
    "github": "GitHub",
}

# Providers we know about. The schema accepts anything but the API
# layer rejects unknown values so a typo doesn't silently land an
# unwireable row.
KNOWN_PROVIDERS: tuple[str, ...] = ("google", "microsoft", "github")


def upsert_env_provider(
    session: Session,
    *,
    provider: str,
    client_id: str | None,
    client_secret: str | None,
) -> AuthProvider | None:
    """Ensure a row for the given env-derived credentials exists.

    Idempotent: if a row already exists for ``(provider, client_id)``,
    its ``client_secret`` is refreshed (the env is the source of
    truth). If ``client_id`` is empty, no-op.

    Returns the upserted row (or ``None`` when the env values are
    incomplete).
    """
    if not client_id or not client_secret:
        return None
    row = session.execute(
        select(AuthProvider).where(
            AuthProvider.provider == provider,
            AuthProvider.client_id == client_id,
        )
    ).scalar_one_or_none()
    now = int(time.time())
    if row is None:
        row = AuthProvider(
            provider=provider,
            label=f"{PROVIDER_LABELS.get(provider, provider).title()} (from .env)",
            client_id=client_id,
            client_secret=client_secret,
            enabled=True,
            source="env",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        logger.info("auth_providers: seeded %s row from .env", provider)
        return row

    # Keep secret + source in sync with .env on every restart so a
    # rotated secret in .env propagates automatically.
    changed = False
    if row.client_secret != client_secret:
        row.client_secret = client_secret
        changed = True
    if row.source != "env":
        row.source = "env"
        changed = True
    if changed:
        row.updated_at = now
    return row


# ---------------------------------------------------------------------------
# Validity check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCheckResult:
    ok: bool
    message: str


# Bogus token endpoint we can hit to probe the credentials. The provider
# rejects the *code* (we send junk), but the error it picks tells us
# whether the *client* was accepted first.
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


async def check_provider(
    *, provider: str, client_id: str, client_secret: str
) -> ProviderCheckResult:
    """Probe ``provider``'s token endpoint with a bogus code.

    For Google, the response's ``error`` field tells us:

    * ``invalid_grant`` — credentials accepted, only the (bogus) code
      was rejected. We treat this as "valid".
    * ``invalid_client`` — client_id and/or client_secret are wrong.
    * anything else — surface the message; admin can read what's wrong.

    Microsoft / GitHub: not implemented yet — the endpoint returns
    ``ok=False`` with a not-implemented message. Admin can still save
    the row; only the validity probe is missing.
    """
    if provider == "google":
        return await _check_google(client_id, client_secret)
    return ProviderCheckResult(
        ok=False, message=f"Validity check not implemented for provider={provider!r}"
    )


async def _check_google(client_id: str, client_secret: str) -> ProviderCheckResult:
    if not client_id.strip() or not client_secret.strip():
        return ProviderCheckResult(ok=False, message="Missing client_id or client_secret")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    # The code value doesn't matter — we expect a 4xx.
                    "code": "voitta_credential_probe_invalid",
                    "grant_type": "authorization_code",
                    # Likewise, the redirect_uri doesn't have to be a
                    # registered one; Google checks the client first.
                    "redirect_uri": "http://localhost/voitta-probe",
                },
            )
    except httpx.HTTPError as e:
        return ProviderCheckResult(
            ok=False, message=f"Network error contacting Google: {e}"
        )

    try:
        body = resp.json()
    except ValueError:
        return ProviderCheckResult(
            ok=False, message=f"Unexpected non-JSON response (HTTP {resp.status_code})"
        )

    err = (body.get("error") or "").strip()
    desc = (body.get("error_description") or "").strip()

    if err == "invalid_grant":
        # The code was rejected, which means the client was accepted
        # first — this is the success signal.
        return ProviderCheckResult(
            ok=True, message="Credentials accepted by Google."
        )
    if err == "invalid_client":
        return ProviderCheckResult(
            ok=False,
            message=desc or "Google rejected the client_id / client_secret.",
        )
    if err:
        return ProviderCheckResult(ok=False, message=desc or err)
    # No error key — we got a token? Shouldn't happen with a junk code,
    # but if it does, treat as "valid" since Google clearly accepted us.
    return ProviderCheckResult(
        ok=True, message="Credentials appear valid (token response was not an error)."
    )
