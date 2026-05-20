"""Admin-only REST surface.

All routes here are guarded by ``admin_user`` — only the *real* user's
``is_admin`` flag matters; impersonation does NOT confer admin rights.

Three concerns covered:

1. **Allowlist / blocklist editing** — the live sign-in gate's source of
   truth lives in plain text files on the data PD; see
   ``services.admin_store``.

2. **User admin status** — flip ``users.is_admin`` for any address that
   has signed in at least once. Bootstrap admins (env-listed
   ``VOITTA_SUPER_ADMINS``) cannot be demoted via this API: their flag
   gets re-stamped on every sign-in.

3. **Impersonation** — set/clear ``session["acting_as_user_id"]``. The
   ``current_user`` dependency reads that and routes the rest of the
   app at the impersonated user's permissions.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import AuthProvider, User
from ...services import admin_store
from ...services import auth_providers as auth_providers_svc
from ...services import indexing_caps
from ...services.acl import CurrentUser
from ..deps import admin_user, db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Allowlist / blocklist
# ---------------------------------------------------------------------------


class AllowlistOut(BaseModel):
    domains: list[str]
    users: list[str]
    blocked: list[str]
    super_admins: list[str]  # read-only — derived from VOITTA_SUPER_ADMINS


class _DomainIn(BaseModel):
    domain: str


class _EmailIn(BaseModel):
    email: EmailStr


@router.get("/allowlist", response_model=AllowlistOut)
def get_allowlist(_: CurrentUser = Depends(admin_user)) -> AllowlistOut:
    from ...config import get_settings

    return AllowlistOut(
        domains=admin_store.list_allowed_domains(),
        users=admin_store.list_allowed_users(),
        blocked=admin_store.list_blocked_users(),
        super_admins=get_settings().super_admin_list(),
    )


@router.post("/allowlist/domains", response_model=AllowlistOut)
def add_domain(
    body: _DomainIn,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_allowed_domain(body.domain)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s added domain %s", me.email, body.domain)
    return get_allowlist(me)


@router.delete("/allowlist/domains/{domain}", response_model=AllowlistOut)
def remove_domain(
    domain: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_domain(domain)
    logger.info("admin: %s removed domain %s", me.email, domain)
    return get_allowlist(me)


@router.post("/allowlist/users", response_model=AllowlistOut)
def add_email(
    body: _EmailIn,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_allowed_user(str(body.email))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s allowed %s", me.email, body.email)
    return get_allowlist(me)


@router.delete("/allowlist/users/{email}", response_model=AllowlistOut)
def remove_email(
    email: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_user(email)
    logger.info("admin: %s removed allowed user %s", me.email, email)
    return get_allowlist(me)


@router.post("/blocklist", response_model=AllowlistOut)
def add_block(
    body: _EmailIn,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_blocked_user(str(body.email))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s blocked %s", me.email, body.email)
    return get_allowlist(me)


@router.delete("/blocklist/{email}", response_model=AllowlistOut)
def remove_block(
    email: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_blocked_user(email)
    logger.info("admin: %s unblocked %s", me.email, email)
    return get_allowlist(me)


# ---------------------------------------------------------------------------
# Users (existing User rows + admin flag)
# ---------------------------------------------------------------------------


class AdminUserOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    is_admin: bool
    is_super_admin: bool


class _AdminFlagIn(BaseModel):
    is_admin: bool


class _CreateUserIn(BaseModel):
    email: EmailStr
    # Default-true: the natural intent of "add a user" via the admin
    # panel is that they can actually sign in. Operator can flip the
    # checkbox off in the UI for the rare "I want a row in the DB but
    # not on the allowlist" case (mostly: legacy data).
    grant_signin: bool = True
    is_admin: bool = False


@router.post("/users", response_model=AdminUserOut)
def create_user(
    body: _CreateUserIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> AdminUserOut:
    """Pre-create a User row and (optionally) mark them admin + allowlist.

    Why this exists: the Users table only ever showed users who had
    signed in at least once, so an admin couldn't pre-grant admin
    powers to a teammate who hadn't logged in yet. This endpoint
    creates the row up front so the flag has somewhere to live, and
    by default also adds the address to ``allowed_users.txt`` so the
    teammate can actually sign in. The admin flag is then just one
    PATCH away — or set in this same call via ``is_admin=True``.
    """
    from ...config import get_settings
    from ...services.acl import get_or_create_user

    email = str(body.email).strip().lower()
    user = get_or_create_user(db, email)
    user.is_admin = bool(body.is_admin) or bool(user.is_admin)
    db.commit()

    if body.grant_signin:
        admin_store.add_allowed_user(email)

    logger.info(
        "admin: %s pre-created %s (admin=%s, signin=%s)",
        me.email, email, body.is_admin, body.grant_signin,
    )

    super_set = {sa.lower() for sa in get_settings().super_admin_list()}
    return AdminUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=bool(user.is_admin),
        is_super_admin=user.email.lower() in super_set,
    )


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(admin_user),
) -> list[AdminUserOut]:
    """Every User row that has ever signed in. Used by the admin UI for
    both the admin-flag toggle and the impersonation dropdown."""
    from ...config import get_settings

    super_set = {sa.lower() for sa in get_settings().super_admin_list()}
    rows = db.execute(select(User).order_by(User.email)).scalars().all()
    return [
        AdminUserOut(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            is_admin=bool(u.is_admin),
            is_super_admin=u.email.lower() in super_set,
        )
        for u in rows
    ]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def set_admin_flag(
    user_id: int,
    body: _AdminFlagIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> AdminUserOut:
    """Toggle ``is_admin`` for any user. Super-admins can't be demoted —
    their flag re-stamps on every sign-in, so attempting to flip it
    silently sticks but won't survive their next login. We could 409
    instead but it's friendlier to let the call succeed and let the
    admin discover that side via the ``is_super_admin`` flag in the
    response."""
    from ...config import get_settings

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    target.is_admin = bool(body.is_admin)
    db.commit()
    logger.info(
        "admin: %s set is_admin=%s for %s", me.email, body.is_admin, target.email
    )
    super_set = {sa.lower() for sa in get_settings().super_admin_list()}
    return AdminUserOut(
        id=target.id,
        email=target.email,
        display_name=target.display_name,
        is_admin=bool(target.is_admin),
        is_super_admin=target.email.lower() in super_set,
    )


# ---------------------------------------------------------------------------
# Impersonation (session-scoped, real-admin-only)
# ---------------------------------------------------------------------------


class ImpersonateOut(BaseModel):
    acting_as_user_id: int | None
    acting_as_email: str | None


@router.post("/impersonate/{user_id}", response_model=ImpersonateOut)
def start_impersonate(
    user_id: int,
    request: Request,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> ImpersonateOut:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target.id == me.id:
        # Pretending to be yourself is a no-op; clear instead so the UI
        # banner disappears.
        request.session.pop("acting_as_user_id", None)
        return ImpersonateOut(acting_as_user_id=None, acting_as_email=None)
    request.session["acting_as_user_id"] = target.id
    logger.info("admin: %s now viewing as %s", me.email, target.email)
    return ImpersonateOut(acting_as_user_id=target.id, acting_as_email=target.email)


@router.delete("/impersonate", response_model=ImpersonateOut)
def stop_impersonate(
    request: Request,
    _: CurrentUser = Depends(admin_user),
) -> ImpersonateOut:
    request.session.pop("acting_as_user_id", None)
    return ImpersonateOut(acting_as_user_id=None, acting_as_email=None)


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
    _: CurrentUser = Depends(admin_user),
) -> list[AuthProviderOut]:
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
    me: CurrentUser = Depends(admin_user),
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
    return _to_out(row)


@router.patch("/auth-providers/{provider_id}", response_model=AuthProviderOut)
def update_auth_provider(
    provider_id: int,
    body: _AuthProviderPatchIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
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
    return _to_out(row)


@router.delete("/auth-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_auth_provider(
    provider_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
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
    # Source='env' rows reappear on the next restart — that's by design.


@router.post("/auth-providers/{provider_id}/check", response_model=_AuthProviderCheckOut)
async def check_auth_provider(
    provider_id: int,
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(admin_user),
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


# ---------------------------------------------------------------------------
# Indexing caps — admin-tunable per-format / per-file limits.
# ---------------------------------------------------------------------------


class IndexingCapsOut(BaseModel):
    values: dict[str, int]
    defaults: dict[str, int]
    bounds: dict[str, list[int]]


@router.get("/indexing-caps", response_model=IndexingCapsOut)
def get_indexing_caps(_: CurrentUser = Depends(admin_user)) -> IndexingCapsOut:
    """Return current cap values plus defaults + bounds for the UI.

    The values reflect the override JSON merged over the shipped defaults
    (and ``Settings``-sourced env defaults for fields that have both).
    The UI renders each row with min/max ``input`` attributes pulled from
    ``bounds`` and a "reset" button that posts the matching ``defaults``
    entry back.
    """
    return IndexingCapsOut(
        values=indexing_caps.as_dict(),
        defaults=indexing_caps.defaults_dict(),
        bounds=indexing_caps.bounds_dict(),
    )


@router.patch("/indexing-caps", response_model=IndexingCapsOut)
def update_indexing_caps(
    body: dict[str, int],
    me: CurrentUser = Depends(admin_user),
) -> IndexingCapsOut:
    """Merge ``body`` (partial) into the persisted override and re-cache.

    Unknown keys are dropped; out-of-bounds values are clamped to the
    declared range in :data:`indexing_caps.BOUNDS`. Non-integer values
    return 400.
    """
    try:
        indexing_caps.update(body)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s updated indexing caps: keys=%s", me.email, sorted(body))
    return IndexingCapsOut(
        values=indexing_caps.as_dict(),
        defaults=indexing_caps.defaults_dict(),
        bounds=indexing_caps.bounds_dict(),
    )


# ---------------------------------------------------------------------------
# Admin-typed settings (currently: NFS root)
# ---------------------------------------------------------------------------


class AdminSettingsOut(BaseModel):
    nfs_root: str
    # ``nfs_available`` is ``nfs_root`` non-empty AND the directory
    # exists + is readable. The sync UI gates the NFS option on this
    # boolean so a mount that disappears flips the feature off without
    # restart.
    nfs_available: bool
    nfs_status: str  # 'disabled' | 'ok' | 'missing' | 'not_a_directory' | 'unreadable'


class _AdminSettingsPatchIn(BaseModel):
    # Only fields actually present in the request body are touched —
    # pass ``{"nfs_root": ""}`` to clear, omit to leave alone. Empty
    # string is a valid value (disables the feature) so we can't use
    # ``None`` as the "leave alone" signal; require the key's presence.
    nfs_root: str | None = None


def _probe_nfs_root(value: str) -> tuple[bool, str]:
    """Classify the configured NFS root for the UI status pill."""
    from pathlib import Path

    if not value:
        return False, "disabled"
    p = Path(value)
    if not p.exists():
        return False, "missing"
    if not p.is_dir():
        return False, "not_a_directory"
    # Smoke-test read access; iterdir on an unreadable mount throws.
    try:
        next(iter(p.iterdir()), None)
    except (PermissionError, OSError):
        return False, "unreadable"
    return True, "ok"


def _admin_settings_out() -> AdminSettingsOut:
    nfs_root = admin_store.get_nfs_root()
    ok, status_str = _probe_nfs_root(nfs_root)
    return AdminSettingsOut(
        nfs_root=nfs_root,
        nfs_available=ok,
        nfs_status=status_str,
    )


@router.get("/settings", response_model=AdminSettingsOut)
def get_admin_settings(_: CurrentUser = Depends(admin_user)) -> AdminSettingsOut:
    return _admin_settings_out()


@router.patch("/settings", response_model=AdminSettingsOut)
def update_admin_settings(
    body: _AdminSettingsPatchIn,
    me: CurrentUser = Depends(admin_user),
) -> AdminSettingsOut:
    """Update one or more typed admin settings.

    ``nfs_root`` is validated at write time. An empty string is
    accepted (turns the feature off); a non-empty path must exist and
    be readable, otherwise 400 — the admin gets immediate feedback
    rather than a delayed "no files found" at sync time. The runtime
    check still re-runs every browse / sync request, so a path that
    disappears after configuration also degrades gracefully.
    """
    updates: dict[str, object] = {}
    if body.nfs_root is not None:
        value = body.nfs_root.strip()
        if value:
            ok, status_str = _probe_nfs_root(value)
            if not ok:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"NFS root {value!r} cannot be used: {status_str}",
                )
        updates["nfs_root"] = value
    if updates:
        admin_store.save_settings(updates)
        logger.info("admin: %s updated settings: keys=%s", me.email, sorted(updates))
    return _admin_settings_out()
