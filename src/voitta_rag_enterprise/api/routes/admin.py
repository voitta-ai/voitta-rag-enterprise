"""Admin-only REST surface.

All routes here are guarded by ``admin_user`` — only the *real* user's
``is_admin`` flag matters; impersonation does NOT confer admin rights.
The one exception is ``GET /auth-providers``, which is read-only and open
to any authenticated user so admin-defined OAuth apps work as shared
sign-in/sync shortcuts; its mutating siblings remain admin-only.

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
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.database import session_scope
from ...db.models import AuthProvider, User
from ...services import admin_store, events
from ...services import auth_providers as auth_providers_svc
from ...services import indexing_caps
from ...services.acl import CurrentUser
from ..deps import admin_user, current_user, db_session

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
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.delete("/allowlist/domains/{domain}", response_model=AllowlistOut)
def remove_domain(
    domain: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_domain(domain)
    logger.info("admin: %s removed domain %s", me.email, domain)
    out = get_allowlist(me)
    publish_admin_state()
    return out


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
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.delete("/allowlist/users/{email}", response_model=AllowlistOut)
def remove_email(
    email: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_user(email)
    logger.info("admin: %s removed allowed user %s", me.email, email)
    out = get_allowlist(me)
    publish_admin_state()
    return out


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
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.delete("/blocklist/{email}", response_model=AllowlistOut)
def remove_block(
    email: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_blocked_user(email)
    logger.info("admin: %s unblocked %s", me.email, email)
    out = get_allowlist(me)
    publish_admin_state()
    return out


# ---------------------------------------------------------------------------
# Users (existing User rows + admin flag)
# ---------------------------------------------------------------------------


class AdminUserOut(BaseModel):
    # One row per ACCOUNT — (email, company_id). The UI groups rows by
    # email so a multi-account person reads as one entry with chips.
    id: int
    email: str
    company_id: str = ""
    company_name: str = ""
    display_name: str | None
    is_admin: bool
    is_super_admin: bool
    groups: list[str] = []
    # Live allowlist check (badge provenance). Display-only.
    native_allowed: bool = False


def _super_set() -> set[str]:
    from ...config import get_settings

    return {sa.lower() for sa in get_settings().super_admin_list()}


def _user_out(
    user: User,
    *,
    groups: list[str],
    supers: set[str] | None = None,
) -> AdminUserOut:
    supers = _super_set() if supers is None else supers
    return AdminUserOut(
        id=user.id,
        email=user.email,
        company_id=user.company_id or "",
        company_name=user.company_name or "",
        display_name=user.display_name,
        is_admin=bool(user.is_admin),
        is_super_admin=user.email.lower() in supers,
        groups=groups,
        native_allowed=admin_store.is_native_allowed(user.email),
    )


class _AdminFlagIn(BaseModel):
    # All optional — a PATCH may flip admin, rename, set groups, or any combo.
    # ``None`` means "leave alone"; for groups, pass [] to clear all memberships.
    is_admin: bool | None = None
    display_name: str | None = None
    groups: list[str] | None = None


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

    out = _user_out(user, groups=[])
    publish_admin_state()
    return out


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(admin_user),
) -> list[AdminUserOut]:
    """Every User row that has ever signed in. Used by the admin UI for
    both the admin-flag toggle and the impersonation dropdown."""
    from ...services import groups as groups_svc

    supers = _super_set()
    by_user = groups_svc.group_names_by_user(db)
    rows = db.execute(
        select(User).order_by(User.email, User.company_id)
    ).scalars().all()
    return [
        _user_out(u, groups=by_user.get(u.id, []), supers=supers) for u in rows
    ]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: int,
    body: _AdminFlagIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> AdminUserOut:
    """Update a user: admin flag, display name, and/or group membership.

    Each field is optional ("leave alone" when omitted). Super-admins can't be
    demoted — their flag re-stamps on every sign-in, so a flip here silently
    sticks but won't survive their next login; we let the call succeed and let
    the admin see ``is_super_admin`` in the response rather than 409.
    """
    from ...services import groups as groups_svc

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if body.is_admin is not None:
        # Person-level: the flag applies to every account of this email.
        from ...services.acl import stamp_person_admin

        stamp_person_admin(db, target.email, bool(body.is_admin))
    if body.display_name is not None:
        target.display_name = body.display_name.strip() or None
    if body.groups is not None:
        groups_svc.set_user_groups(db, user_id, body.groups)
    db.commit()
    logger.info(
        "admin: %s updated user %s (admin=%s, name=%s, groups=%s)",
        me.email, target.email, body.is_admin, body.display_name, body.groups,
    )
    out = _user_out(target, groups=groups_svc.group_names_for_user(db, user_id))
    publish_admin_state()
    return out


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> None:
    """Delete a user. Memberships / api_keys / folder_acl cascade; owned
    folders' ``owner_id`` is set null per schema. Guards: can't delete a
    super-admin (they'd just be re-created on next sign-in, and it reads as a
    footgun) nor yourself (lock-out protection)."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target.id == me.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You can't delete your own account."
        )
    if target.email.lower() in _super_set():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Super-admins (VOITTA_SUPER_ADMINS) can't be deleted here.",
        )
    email = target.email
    db.delete(target)
    db.commit()
    logger.info("admin: %s deleted user %s", me.email, email)
    publish_admin_state()


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


class _ClerkImpersonateIn(BaseModel):
    email: EmailStr
    # '' = the user's Personal account; a Clerk org id targets that
    # company account (so "View as" from a company card lands the
    # impersonation in that company's scope).
    company_id: str = ""


@router.post("/clerk/impersonate", response_model=ImpersonateOut)
async def clerk_impersonate(
    body: _ClerkImpersonateIn,
    request: Request,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> ImpersonateOut:
    """Impersonate a Clerk-directory user — SUPER-ADMIN only.

    Unlike the by-user-id route above, the target may never have signed
    in: we pull the directory fresh (never cached), verify the email is
    really in Clerk, provision their accounts exactly like the login
    callback would (Personal + one per org), then impersonate the
    requested account. Stop via the normal DELETE /impersonate.
    """
    from ...services import clerk as clerk_svc
    from ...services.acl import get_or_create_user
    from ...services.admin_store import is_super_admin

    if not is_super_admin(me.email):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Impersonating Clerk users requires super-admin (VOITTA_SUPER_ADMINS).",
        )
    if not admin_store.get_clerk_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Clerk mode is not enabled.")
    key = admin_store.get_clerk_secret_key()
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No Clerk secret key configured.")

    email = str(body.email).strip().lower()
    try:
        directory = await clerk_svc.fetch_directory(key)
    except clerk_svc.ClerkError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    match = next(
        (u for u in directory["users"]
         if (u.get("email") or "").strip().lower() == email),
        None,
    )
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a Clerk-directory user")

    # Provision like the login callback: Personal + one account per org.
    display_name = match.get("name") or ""
    personal = get_or_create_user(db, email)
    if not personal.display_name and display_name:
        personal.display_name = display_name
    by_company = {"": personal}
    for org in match.get("orgs") or []:
        if not org.get("id"):
            continue
        acc = get_or_create_user(db, email, org["id"], org.get("name", ""))
        if not acc.display_name and display_name:
            acc.display_name = display_name
        by_company[org["id"]] = acc
    db.commit()

    target = by_company.get(body.company_id or "")
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That user is not a member of the requested company.",
        )
    request.session["acting_as_user_id"] = target.id
    logger.info(
        "admin: %s now viewing as clerk user %s (account=%d, company=%s)",
        me.email, email, target.id, target.company_name or "Personal",
    )
    publish_admin_state()  # new account rows may have appeared in Users
    return ImpersonateOut(acting_as_user_id=target.id, acting_as_email=email)


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
    publish_admin_state()
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
    publish_admin_state()
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
    publish_admin_state()
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
    publish_admin_state()
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
    # Directory toggles. ``native_directory_enabled`` shows/hides the local
    # Users + Groups tabs; ``clerk_enabled`` the read-only Clerk Users +
    # Companies tabs. Independent — any combination is valid. Display-only:
    # neither affects sign-in or authorization.
    native_directory_enabled: bool
    # ``clerk_secret_key`` is the *effective* key (admin-stored value,
    # falling back to CLERK_SECRET_KEY from .env) — plaintext, same posture
    # as auth-provider secrets: this endpoint is admin-gated and admins can
    # already read .env. ``clerk_key_from_env`` tells the UI to badge the
    # pre-filled value.
    clerk_enabled: bool
    clerk_secret_key: str
    clerk_key_from_env: bool


class _AdminSettingsPatchIn(BaseModel):
    # Only fields actually present in the request body are touched —
    # pass ``{"nfs_root": ""}`` to clear, omit to leave alone. Empty
    # string is a valid value (disables the feature) so we can't use
    # ``None`` as the "leave alone" signal; require the key's presence.
    nfs_root: str | None = None
    native_directory_enabled: bool | None = None
    clerk_enabled: bool | None = None
    # Empty string clears the stored key (the .env fallback, if any,
    # then takes over again).
    clerk_secret_key: str | None = None


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
        native_directory_enabled=admin_store.get_native_directory_enabled(),
        clerk_enabled=admin_store.get_clerk_enabled(),
        clerk_secret_key=admin_store.get_clerk_secret_key(),
        clerk_key_from_env=admin_store.clerk_key_from_env(),
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
    if body.native_directory_enabled is not None:
        updates["native_directory_enabled"] = bool(body.native_directory_enabled)
    if body.clerk_enabled is not None:
        if body.clerk_enabled and not (
            (body.clerk_secret_key or "").strip()
            or admin_store.get_clerk_secret_key()
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Set a Clerk secret key (sk_…) before enabling Clerk mode.",
            )
        updates["clerk_enabled"] = bool(body.clerk_enabled)
    if body.clerk_secret_key is not None:
        value = body.clerk_secret_key.strip()
        if value and not value.startswith("sk_"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Clerk secret keys start with sk_test_ or sk_live_.",
            )
        # Storing the same value .env provides would shadow future .env
        # rotations for no benefit — keep the store empty in that case.
        from ...config import get_settings

        if value == (get_settings().clerk_secret_key or "").strip():
            value = ""
        updates["clerk_secret_key"] = value
    if updates:
        admin_store.save_settings(updates)
        logger.info("admin: %s updated settings: keys=%s", me.email, sorted(updates))
        publish_admin_state()
    return _admin_settings_out()


# ---------------------------------------------------------------------------
# Clerk directory (read-only) — live proxy to the Clerk Backend API.
# ---------------------------------------------------------------------------


@router.get("/clerk/directory")
async def get_clerk_directory(
    _: CurrentUser = Depends(admin_user),
) -> dict:
    """Users + organizations + memberships from Clerk, UI-ready.

    Fetched live on every call (no caching): the admin view is
    low-traffic and staleness would be more confusing than the
    ~1 s round-trip. 400 when Clerk mode is off or no key is set;
    502 when Clerk itself rejects us.
    """
    from ...services import clerk as clerk_svc

    if not admin_store.get_clerk_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Clerk mode is not enabled.")
    key = admin_store.get_clerk_secret_key()
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No Clerk secret key configured.")
    try:
        return await clerk_svc.fetch_directory(key)
    except clerk_svc.ClerkError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e


# ---------------------------------------------------------------------------
# Groups (organizational; no folder-ACL effect)
# ---------------------------------------------------------------------------


class GroupOut(BaseModel):
    id: int
    name: str
    description: str | None
    member_count: int


class _GroupCreateIn(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None


class _GroupPatchIn(BaseModel):
    name: str | None = None
    description: str | None = None


class _MemberIn(BaseModel):
    user_id: int


@router.get("/groups", response_model=list[GroupOut])
def list_groups(
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(admin_user),
) -> list[GroupOut]:
    from ...services import groups as groups_svc

    return [GroupOut(**g) for g in groups_svc.list_groups_with_counts(db)]


@router.post("/groups", response_model=GroupOut)
def create_group(
    body: _GroupCreateIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> GroupOut:
    from ...db.models import Group

    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
    existing = db.execute(select(Group).where(Group.name == name)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Group {name!r} already exists")
    grp = Group(name=name, description=(body.description or "").strip() or None)
    db.add(grp)
    db.commit()
    db.refresh(grp)
    logger.info("admin: %s created group %s", me.email, name)
    out = GroupOut(id=grp.id, name=grp.name, description=grp.description, member_count=0)
    publish_admin_state()
    return out


@router.patch("/groups/{group_id}", response_model=GroupOut)
def update_group(
    group_id: int,
    body: _GroupPatchIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> GroupOut:
    from ...db.models import Group
    from ...services import groups as groups_svc

    grp = db.get(Group, group_id)
    if grp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name can't be empty")
        clash = db.execute(
            select(Group).where(Group.name == new_name, Group.id != group_id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Group {new_name!r} already exists")
        grp.name = new_name
    if body.description is not None:
        grp.description = body.description.strip() or None
    db.commit()
    logger.info("admin: %s updated group id=%d", me.email, group_id)
    count = len(groups_svc.group_member_ids(db, group_id))
    out = GroupOut(id=grp.id, name=grp.name, description=grp.description, member_count=count)
    publish_admin_state()
    return out


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> None:
    from ...db.models import Group

    grp = db.get(Group, group_id)
    if grp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    name = grp.name
    db.delete(grp)  # memberships cascade
    db.commit()
    logger.info("admin: %s deleted group %s", me.email, name)
    publish_admin_state()


@router.post("/groups/{group_id}/members", status_code=status.HTTP_204_NO_CONTENT)
def add_group_member(
    group_id: int,
    body: _MemberIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> None:
    from ...db.models import Group
    from ...services import groups as groups_svc

    if db.get(Group, group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    if db.get(User, body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    groups_svc.add_member(db, group_id, body.user_id)
    db.commit()
    publish_admin_state()


@router.delete(
    "/groups/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_group_member(
    group_id: int,
    user_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> None:
    from ...services import groups as groups_svc

    groups_svc.remove_member(db, group_id, user_id)
    db.commit()
    publish_admin_state()


# ---------------------------------------------------------------------------
# WebSocket snapshot + push
#
# The admin modal is WS-backed: it renders from a single ``admin.snapshot``
# frame (sent on connect to admins, see ``api.snapshot``) and re-renders on the
# same frame pushed after every admin mutation above. No HTTP-on-open, no
# post-mutation refetch. Delivery is admin-only (the WS pump drops ``admin.*``
# events for non-admin connections).
# ---------------------------------------------------------------------------


def build_admin_state(db: Session) -> dict:
    """Full admin-console state, mirroring the admin GET endpoints.

    The shape matches what the SPA's admin modal renders so one builder feeds
    both the connect snapshot and the on-mutation push.
    """
    from ...config import get_settings
    from ...services import groups as groups_svc

    super_list = get_settings().super_admin_list()
    supers = {sa.lower() for sa in super_list}
    by_user = groups_svc.group_names_by_user(db)
    users = db.execute(
        select(User).order_by(User.email, User.company_id)
    ).scalars().all()
    providers = db.execute(select(AuthProvider).order_by(AuthProvider.id)).scalars().all()
    return {
        "allowlist": AllowlistOut(
            domains=admin_store.list_allowed_domains(),
            users=admin_store.list_allowed_users(),
            blocked=admin_store.list_blocked_users(),
            super_admins=super_list,
        ).model_dump(),
        "users": [
            _user_out(u, groups=by_user.get(u.id, []), supers=supers).model_dump()
            for u in users
        ],
        "groups": groups_svc.list_groups_with_counts(db),
        "auth_providers": [_to_out(r).model_dump() for r in providers],
        "indexing_caps": IndexingCapsOut(
            values=indexing_caps.as_dict(),
            defaults=indexing_caps.defaults_dict(),
            bounds=indexing_caps.bounds_dict(),
        ).model_dump(),
        "settings": _admin_settings_out().model_dump(),
    }


def publish_admin_state() -> None:
    """Push the full admin state to every admin WS connection.

    Low-volume (admin mutations are rare), so re-sending the whole state on each
    change keeps the client logic to a single replace with no delta merging.
    """
    with session_scope() as db:
        state = build_admin_state(db)
    events.publish("admin", {"type": "admin.snapshot", "state": state})
