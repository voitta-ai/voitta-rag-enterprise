"""Admin-typed settings (NFS root, directory toggles) + Clerk directory proxy."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel

from ....services import admin_store
from ....services.acl import CurrentUser
from ...deps import admin_user, super_admin_user
from .base import publish_admin_state, router

logger = logging.getLogger(__name__)


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
    me: CurrentUser = Depends(super_admin_user),
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
        from ....config import get_settings

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
    me: CurrentUser = Depends(admin_user),
) -> dict:
    """Users + organizations + memberships from Clerk, UI-ready.

    Fetched live on every call (no caching): the admin view is
    low-traffic and staleness would be more confusing than the
    ~1 s round-trip. 400 when Clerk mode is off or no key is set;
    502 when Clerk itself rejects us.

    Scoped: a superadmin sees the whole directory; a regular admin sees only
    the orgs they administer (role=admin) and those orgs' members — mirroring
    the users-list scoping so the Clerk tab can't leak other orgs.
    """
    from ....services import clerk as clerk_svc
    from ....services.admin_scope import admin_orgs_from_directory
    from ....services.admin_store import is_super_admin

    if not admin_store.get_clerk_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Clerk mode is not enabled.")
    key = admin_store.get_clerk_secret_key()
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No Clerk secret key configured.")
    try:
        directory = await clerk_svc.fetch_directory(key)
    except clerk_svc.ClerkError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    if is_super_admin(me.email):
        return directory

    # Regular admin: restrict to the orgs they administer (reuse the same
    # pure derivation the scope resolver uses — no second Clerk sweep).
    admin_org_ids, _names = admin_orgs_from_directory(directory, me.email)
    orgs = [o for o in directory.get("organizations", []) if o.get("id") in admin_org_ids]
    visible_emails = {
        (m.get("email") or "").strip().lower()
        for o in orgs
        for m in o.get("members", [])
    }
    users = [
        u
        for u in directory.get("users", [])
        if (u.get("email") or "").strip().lower() in visible_emails
    ]
    return {"users": users, "organizations": orgs}
