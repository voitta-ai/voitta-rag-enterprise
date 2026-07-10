"""Auth providers — admin-managed OAuth credentials catalog."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....db.models import AuthProvider
from ....services import auth_providers as auth_providers_svc
from ....services.acl import CurrentUser
from ...deps import current_user, db_session, super_admin_user
from .base import publish_admin_state, router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth providers (admin-managed OAuth credentials catalog)
# ---------------------------------------------------------------------------
#
# This is just a list. The login flow currently reads
# ``Settings.google_auth_*`` from .env and is unaffected. Two rows for
# the same provider are allowed; the schema only deduplicates by id.


class AuthProviderOut(BaseModel):
    id: int
    provider: str
    label: str
    client_id: str
    # Plaintext: same posture as .env. The endpoint is admin-gated and
    # admins already have access to the .env-stored values, so masking
    # in transport adds no defense.
    client_secret: str
    tenant_id: str = ""  # Microsoft only; empty for Google/GitHub
    enabled: bool
    # ``source='env'`` rows are re-created on every restart by the
    # bootstrap upsert; the UI surfaces a small "from .env" pill so the
    # admin understands why a deleted row reappears next reboot.
    source: str
    created_at: int
    updated_at: int


class _AuthProviderCreateIn(BaseModel):
    provider: str
    label: str = ""
    client_id: str
    client_secret: str = ""
    tenant_id: str = ""
    enabled: bool = True


class _AuthProviderPatchIn(BaseModel):
    # Every field optional — PATCH semantics. ``None`` means "leave
    # alone"; an explicit empty string clears the field.
    label: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    tenant_id: str | None = None
    enabled: bool | None = None


class _AuthProviderCheckOut(BaseModel):
    ok: bool
    message: str


def _to_out(row: AuthProvider) -> AuthProviderOut:
    return AuthProviderOut(
        id=row.id,
        provider=row.provider,
        label=row.label,
        client_id=row.client_id,
        client_secret=row.client_secret,
        tenant_id=row.tenant_id or "",
        enabled=bool(row.enabled),
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _normalise_provider(value: str) -> str:
    v = (value or "").strip().lower()
    if v not in auth_providers_svc.KNOWN_PROVIDERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown provider {value!r}. Known: "
            + ", ".join(auth_providers_svc.KNOWN_PROVIDERS),
        )
    return v


@router.get("/auth-providers", response_model=list[AuthProviderOut])
def list_auth_providers(
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(current_user),
) -> list[AuthProviderOut]:
    # Read-only and open to any authenticated user *by design*: an admin
    # defines OAuth apps once, and every user picks them as sign-in/sync
    # shortcuts. The mutating routes below stay admin-only.
    rows = (
        db.execute(select(AuthProvider).order_by(AuthProvider.id))
        .scalars()
        .all()
    )
    return [_to_out(r) for r in rows]


@router.post("/auth-providers", response_model=AuthProviderOut)
def create_auth_provider(
    body: _AuthProviderCreateIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(super_admin_user),
) -> AuthProviderOut:
    import time

    provider = _normalise_provider(body.provider)
    if not body.client_id.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "client_id is required")
    tenant_id = body.tenant_id.strip()
    if provider == "microsoft" and not tenant_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Microsoft providers require a tenant_id (Azure AD tenant id "
            "or *.onmicrosoft.com domain).",
        )
    label = body.label.strip() or auth_providers_svc.PROVIDER_LABELS.get(provider, provider).title()
    now = int(time.time())
    row = AuthProvider(
        provider=provider,
        label=label,
        client_id=body.client_id.strip(),
        client_secret=body.client_secret,
        tenant_id=tenant_id,
        enabled=bool(body.enabled),
        source="user",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    db.commit()
    logger.info(
        "admin: %s created auth provider id=%d provider=%s", me.email, row.id, provider
    )
    publish_admin_state()
    return _to_out(row)


@router.patch("/auth-providers/{provider_id}", response_model=AuthProviderOut)
def update_auth_provider(
    provider_id: int,
    body: _AuthProviderPatchIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(super_admin_user),
) -> AuthProviderOut:
    import time

    row = db.get(AuthProvider, provider_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Auth provider not found")
    if body.label is not None:
        row.label = body.label.strip()
    if body.client_id is not None:
        new_id = body.client_id.strip()
        if not new_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "client_id cannot be empty"
            )
        row.client_id = new_id
    if body.client_secret is not None:
        row.client_secret = body.client_secret
    if body.tenant_id is not None:
        new_tenant = body.tenant_id.strip()
        if row.provider == "microsoft" and not new_tenant:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Microsoft providers require a non-empty tenant_id.",
            )
        row.tenant_id = new_tenant
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
    row.updated_at = int(time.time())
    db.commit()
    logger.info(
        "admin: %s updated auth provider id=%d", me.email, provider_id
    )
    publish_admin_state()
    return _to_out(row)


@router.delete("/auth-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_auth_provider(
    provider_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(super_admin_user),
) -> None:
    row = db.get(AuthProvider, provider_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Auth provider not found")
    db.delete(row)
    db.commit()
    logger.info(
        "admin: %s deleted auth provider id=%d (provider=%s, source=%s)",
        me.email, provider_id, row.provider, row.source,
    )
    publish_admin_state()
    # Source='env' rows reappear on the next restart — that's by design.


@router.post("/auth-providers/{provider_id}/check", response_model=_AuthProviderCheckOut)
async def check_auth_provider(
    provider_id: int,
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(super_admin_user),
) -> _AuthProviderCheckOut:
    """Probe the provider's token endpoint with the stored credentials.

    For Google this distinguishes ``invalid_client`` (creds wrong) from
    ``invalid_grant`` (creds fine, code is bogus). Other providers
    return ``ok=False`` with a not-implemented message until wired up.
    """
    row = db.get(AuthProvider, provider_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Auth provider not found")
    result = await auth_providers_svc.check_provider(
        provider=row.provider,
        client_id=row.client_id,
        client_secret=row.client_secret,
        tenant_id=row.tenant_id or "",
    )
    return _AuthProviderCheckOut(ok=result.ok, message=result.message)
