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


class ClerkInstanceOut(BaseModel):
    """One NAMED Clerk instance ("Development", "Production", …).

    ``secret_key`` is plaintext — same admin-gated posture as auth-provider
    secrets. ``from_env`` marks the Development card whose key comes from
    ``CLERK_SECRET_KEY`` in .env (read-only in the UI; never persisted).
    ``live`` is derived from the key prefix (sk_live_) for the env badge.
    """

    name: str
    secret_key: str
    enabled: bool
    from_env: bool = False
    live: bool = False


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
    # Named Clerk instances — the source of truth.
    clerk_instances: list[ClerkInstanceOut]
    # DEPRECATED legacy pair, kept one release for external readers:
    # ``clerk_enabled`` = any instance enabled; ``clerk_secret_key`` =
    # the "Development" instance's key (else first enabled).
    clerk_enabled: bool
    clerk_secret_key: str
    clerk_key_from_env: bool


class ClerkInstanceIn(BaseModel):
    name: str
    secret_key: str = ""
    enabled: bool = False


class _AdminSettingsPatchIn(BaseModel):
    # Only fields actually present in the request body are touched —
    # pass ``{"nfs_root": ""}`` to clear, omit to leave alone. Empty
    # string is a valid value (disables the feature) so we can't use
    # ``None`` as the "leave alone" signal; require the key's presence.
    nfs_root: str | None = None
    native_directory_enabled: bool | None = None
    # Full-list replacement for the named instances (the cards UI always
    # sends the complete list). Validated per card below.
    clerk_instances: list[ClerkInstanceIn] | None = None
    # DEPRECATED legacy pair — still accepted; mapped onto the
    # "Development" instance so old clients keep working.
    clerk_enabled: bool | None = None
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
        clerk_instances=[
            ClerkInstanceOut(
                name=str(i["name"]),
                secret_key=str(i["secret_key"]),
                enabled=bool(i["enabled"]),
                from_env=bool(i.get("from_env")),
                live=str(i["secret_key"]).startswith("sk_live_"),
            )
            for i in admin_store.get_clerk_instances()
        ],
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

    # Named instances — full-list replacement, validated per card.
    instances_to_save: list[dict[str, object]] | None = None
    if body.clerk_instances is not None:
        from ....config import get_settings

        env_key = (get_settings().clerk_secret_key or "").strip()
        seen: set[str] = set()
        cleaned: list[dict[str, object]] = []
        for inst in body.clerk_instances:
            name = inst.name.strip()
            if not name:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Every Clerk instance must have a name.",
                )
            if len(name) > admin_store.CLERK_INSTANCE_NAME_MAX:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Instance name {name!r} is too long "
                    f"(max {admin_store.CLERK_INSTANCE_NAME_MAX} chars).",
                )
            if name.lower() in seen:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Duplicate Clerk instance name {name!r}.",
                )
            seen.add(name.lower())
            key = inst.secret_key.strip()
            if key and not key.startswith("sk_"):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Instance {name!r}: Clerk secret keys start with "
                    "sk_test_ or sk_live_.",
                )
            from_env = (
                name == admin_store.LEGACY_INSTANCE_NAME
                and bool(env_key)
                and key in ("", env_key)
            )
            if inst.enabled and not (key or from_env):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Instance {name!r}: set a secret key before enabling.",
                )
            cleaned.append(
                {
                    "name": name,
                    "secret_key": env_key if (from_env and not key) else key,
                    "enabled": bool(inst.enabled),
                    **({"from_env": True} if from_env else {}),
                }
            )
        instances_to_save = cleaned

    # DEPRECATED legacy pair → mapped onto the "Development" instance so
    # pre-instances clients (and tests) keep working unchanged.
    if instances_to_save is None and (
        body.clerk_enabled is not None or body.clerk_secret_key is not None
    ):
        instances = admin_store.get_clerk_instances()
        dev = next(
            (i for i in instances if i["name"] == admin_store.LEGACY_INSTANCE_NAME),
            None,
        )
        if dev is None:
            dev = {
                "name": admin_store.LEGACY_INSTANCE_NAME,
                "secret_key": "",
                "enabled": False,
            }
            instances.append(dev)
        if body.clerk_secret_key is not None:
            value = body.clerk_secret_key.strip()
            if value and not value.startswith("sk_"):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Clerk secret keys start with sk_test_ or sk_live_.",
                )
            from ....config import get_settings

            if value == (get_settings().clerk_secret_key or "").strip():
                # Same value .env provides — keep the store empty so .env
                # rotations aren't shadowed (from_env re-applies on read).
                dev["secret_key"] = ""
                dev.pop("from_env", None)
            else:
                dev["secret_key"] = value
                dev.pop("from_env", None)
        if body.clerk_enabled is not None:
            if body.clerk_enabled and not (
                str(dev.get("secret_key") or "").strip()
                or admin_store.get_clerk_secret_key()
            ):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Set a Clerk secret key (sk_…) before enabling Clerk mode.",
                )
            dev["enabled"] = bool(body.clerk_enabled)
        instances_to_save = instances

    if updates:
        admin_store.save_settings(updates)
    if instances_to_save is not None:
        admin_store.save_clerk_instances(instances_to_save)
    if updates or instances_to_save is not None:
        logger.info(
            "admin: %s updated settings: keys=%s%s",
            me.email,
            sorted(updates),
            " +clerk_instances" if instances_to_save is not None else "",
        )
        publish_admin_state()
    return _admin_settings_out()


# ---------------------------------------------------------------------------
# Clerk directory (read-only) — live proxy to the Clerk Backend API.
# ---------------------------------------------------------------------------


@router.get("/clerk/directory")
async def get_clerk_directory(
    me: CurrentUser = Depends(admin_user),
) -> dict:
    """Users + organizations + memberships from every enabled instance.

    Shape: ``{"instances": [{name, live, ok, error, users, organizations}]}``
    — one entry per enabled instance, fetched live and concurrently (no
    caching: the admin view is low-traffic and staleness would be more
    confusing than the ~1 s round-trip). Per-instance fail-soft: an
    unreachable instance reports ``ok=False`` + its error while the others
    still render — the UI shows a per-instance warning strip instead of a
    blank tab. 400 only when NO instance is enabled.

    Scoped: a superadmin sees every directory in full; a regular admin sees
    only the orgs they administer (role=admin) in each instance and those
    orgs' members — mirroring the users-list scoping.
    """
    import asyncio as _asyncio

    from ....services import clerk as clerk_svc
    from ....services.admin_scope import admin_orgs_from_directory
    from ....services.admin_store import is_super_admin

    instances = admin_store.enabled_clerk_instances()
    if not instances:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No Clerk instance is enabled."
        )

    results = await _asyncio.gather(
        *(clerk_svc.fetch_directory(str(i["secret_key"])) for i in instances),
        return_exceptions=True,
    )

    super_admin = is_super_admin(me.email)
    out: list[dict] = []
    for inst, res in zip(instances, results, strict=True):
        entry: dict = {
            "name": str(inst["name"]),
            "live": str(inst["secret_key"]).startswith("sk_live_"),
            "ok": not isinstance(res, BaseException),
            "error": str(res) if isinstance(res, BaseException) else "",
            "users": [],
            "organizations": [],
        }
        if not isinstance(res, BaseException):
            if super_admin:
                entry["users"] = res.get("users", [])
                entry["organizations"] = res.get("organizations", [])
            else:
                admin_org_ids, _names = admin_orgs_from_directory(res, me.email)
                orgs = [
                    o
                    for o in res.get("organizations", [])
                    if o.get("id") in admin_org_ids
                ]
                visible_emails = {
                    (m.get("email") or "").strip().lower()
                    for o in orgs
                    for m in o.get("members", [])
                }
                entry["organizations"] = orgs
                entry["users"] = [
                    u
                    for u in res.get("users", [])
                    if (u.get("email") or "").strip().lower() in visible_emails
                ]
        out.append(entry)
    return {"instances": out}
