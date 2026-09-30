"""Read-only views of the app's settings, for the in-app assistant.

What a user could find by clicking through Settings, the folder share /
sync dialogs and (for admins) the admin console, assembled into plain
dicts an LLM can reason over. Every view applies the SAME access rule as
the screen it mirrors:

* account + keys ........ the viewer's own account (Settings modal)
* folder settings ....... any visible folder; the share list only for its
                          owner (share modal is owner-only)
* sync credentials ...... the account's company (company credentials list)
* admin views ........... person-level admins, scoped by their resolved
                          :class:`AdminScope` exactly like the admin console

Secrets never leave this module. Every view copies an explicit ALLOWLIST of
fields — no ORM row or route response model is dumped wholesale — because
some of those models legitimately carry plaintext secrets for the admin UI
(OAuth client secrets, Clerk secret keys), and those must never be sent to
an LLM provider. A new secret column is therefore excluded by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import (
    ApiKey,
    AuthProvider,
    CompanyApiKey,
    Folder,
    FolderDirMeta,
    FolderSyncSource,
    FolderUserSettings,
    Group,
    SyncCredential,
    User,
    UserGroup,
)
from . import admin_store, groups, indexing_caps
from .acl import (
    CurrentUser,
    can_write_folder,
    folder_active_for_user,
    is_folder_owner,
    offered_accounts_for_email,
    person_is_admin,
)
from .admin_scope import AdminScope, filter_users_for_scope, scoped_clerk_directory, user_in_scope
from .retrieval import Viewer
from .sharing_view import sharing_view

# Non-secret runtime configuration an admin may want to reason about.
# Deliberately excludes every credential-bearing setting (session/secret
# keys, OAuth + Clerk secrets, the assistant API key) and connection
# strings that can embed credentials (VOITTA_QDRANT_URL).
_RUNTIME_FIELDS = (
    "single_user",
    "public_base_url",
    "root_path",
    "data_dir",
    "qdrant_mode",
    "dense_model",
    "sparse_model",
    "image_model",
    "max_file_bytes",
    "pdf_parse_method",
    "pdf_lang",
    "pdf_render_pages",
    "cloud_materialize_on_index",
    "session_max_age_seconds",
    "cookie_secure",
    "assistant_max_concurrent_turns",
    "assistant_max_tool_rounds",
    "assistant_turn_timeout_s",
)


def _iso(epoch: int | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _account(u: CurrentUser | User) -> dict[str, Any]:
    return {
        "account_id": u.id,
        "email": u.email,
        "company": getattr(u, "company_name", "") or "Personal",
        "company_id": getattr(u, "company_id", "") or "",
    }


# --- the viewer's own account ---------------------------------------------


def account_overview(db: Session, real: CurrentUser, effective: CurrentUser) -> dict[str, Any]:
    """Who is asking, which account is active, and that account's settings."""
    impersonating = real.id != effective.id
    mcp_opt_outs = [
        {"folder_id": fid, "display_name": name}
        for fid, name in db.execute(
            select(Folder.id, Folder.display_name)
            .join(FolderUserSettings, FolderUserSettings.folder_id == Folder.id)
            .where(FolderUserSettings.user_id == effective.id, FolderUserSettings.active.is_(False))
            .order_by(Folder.display_name)
        ).all()
    ]
    keys = db.execute(
        select(ApiKey)
        .where(ApiKey.user_id == effective.id)
        .order_by(ApiKey.created_at.desc())
    ).scalars()
    return {
        "person": {
            "email": real.email,
            "is_admin": person_is_admin(db, real.email),
            "is_super_admin": admin_store.is_super_admin(real.email),
        },
        "impersonating": impersonating,
        "active_account": {
            **_account(effective),
            "groups": groups.group_names_for_user(db, effective.id),
        },
        # Accounts the person can switch between (Personal / companies).
        "accounts": [
            {**_account(a), "active": a.id == real.id}
            for a in offered_accounts_for_email(db, real.email)
        ],
        # Personal MCP API keys of the active account — names and usage only.
        "api_keys": [
            {"name": k.name, "created_at": _iso(k.created_at), "last_used_at": _iso(k.last_used_at)}
            for k in keys
        ],
        # Folders this account has switched off for external MCP clients.
        "mcp_disabled_folders": mcp_opt_outs,
    }


# --- one folder --------------------------------------------------------------


def folder_settings(db: Session, viewer: Viewer, folder_id: int) -> dict[str, Any]:
    """General settings of a visible folder; its share list for the owner.
    Raises ``ValueError`` when the folder is not visible."""
    folder = db.get(Folder, folder_id)
    if folder is None or not viewer.can_see_folder(db, folder_id):
        raise ValueError(f"Folder {folder_id} not found")
    uid = viewer.user_id
    owned = uid is None or is_folder_owner(db, folder_id, uid)
    owner = db.get(User, folder.owner_id) if folder.owner_id else None
    src = db.get(FolderSyncSource, folder_id)
    descriptions = [
        {"subfolder": m.subpath or "(folder root)", "description": m.description}
        for m in db.execute(
            select(FolderDirMeta)
            .where(FolderDirMeta.folder_id == folder_id, FolderDirMeta.description != "")
            .order_by(FolderDirMeta.subpath)
        ).scalars()
    ]
    out: dict[str, Any] = {
        "folder_id": folder.id,
        "display_name": folder.display_name,
        "path": folder.path,
        "created_at": _iso(folder.created_at),
        "enabled": bool(folder.enabled),
        "owner_email": owner.email if owner else None,
        "owned_by_you": owned,
        "you_can_write_files": owned or (uid is not None and can_write_folder(db, folder_id, uid)),
        "shared_with_community": bool(folder.shared),
        "active_for_your_mcp_clients": uid is None or folder_active_for_user(db, folder_id, uid),
        "sync_source": src.source_type if src is not None else None,
        "descriptions": descriptions,
    }
    if owned:
        out["sharing"] = sharing_view(db, folder).model_dump()
    else:
        out["sharing"] = "visible to the folder owner only"
    return out


# --- company sync credentials ------------------------------------------------------


def sync_credentials(db: Session, account: CurrentUser) -> list[dict[str, Any]]:
    """The reusable sync credentials of the account's company (secrets
    reduced to booleans, as in the credentials list)."""
    rows = db.execute(
        select(SyncCredential)
        .where(SyncCredential.company_id == account.company_id)
        .order_by(SyncCredential.created_at.desc())
    ).scalars()
    out = []
    for c in rows:
        used_by = db.execute(
            select(func.count(FolderSyncSource.folder_id)).where(
                FolderSyncSource.gd_credential_id == c.id
            )
        ).scalar_one()
        out.append({
            "credential_id": c.id,
            "kind": c.kind,
            "label": c.label or "",
            "client_id": c.client_id or "",
            "has_client_secret": bool(c.client_secret),
            "has_service_account": bool(c.service_account_json),
            "connected": bool(c.refresh_token),
            "connected_email": c.connected_email or "",
            "created_by": c.created_by or "",
            "created_at": _iso(c.created_at),
            "used_by_folders": used_by,
        })
    return out


# --- admin console -----------------------------------------------------------------


def _runtime() -> dict[str, Any]:
    s = get_settings()
    out = {name: getattr(s, name) for name in _RUNTIME_FIELDS}
    for name in ("root_path", "data_dir"):
        out[name] = str(out[name]) if out[name] is not None else None
    out["workers"] = s.resolved_workers()
    out["google_sign_in_configured"] = s.google_auth_enabled
    return out


def admin_overview(db: Session, scope: AdminScope, account: CurrentUser) -> dict[str, Any]:
    """The admin console's deployment-wide settings, as the admin sees them."""
    from .assistant.credentials import deployment_configured
    from .assistant.policy import load_policy

    nfs_root = admin_store.get_nfs_root()
    nfs_ok, nfs_status = admin_store.probe_directory(nfs_root)
    link_root = admin_store.get_link_root()
    link_ok, link_status = admin_store.probe_directory(link_root)
    users = db.execute(select(User)).scalars().all()
    company_keys = db.execute(
        select(CompanyApiKey)
        .where(CompanyApiKey.company_id == account.company_id)
        .order_by(CompanyApiKey.created_at.desc())
    ).scalars()
    return {
        "your_permissions": {
            "super_admin": scope.is_super,
            "native_admin": scope.is_native_admin,
            "administered_companies": sorted(scope.admin_org_names),
            # Regular admins may view deployment settings but not change them.
            "can_change_deployment_settings": scope.is_super,
            "company_directory_unreachable": scope.clerk_degraded,
        },
        "users_in_your_scope": len(filter_users_for_scope(scope, users)),
        "groups": db.execute(select(func.count(Group.id))).scalar_one(),
        "sign_in_access": {
            "allowed_domains": admin_store.list_allowed_domains(),
            "allowed_emails": admin_store.list_allowed_users(),
            "blocked_emails": admin_store.list_blocked_users(),
            "super_admins": get_settings().super_admin_list(),
        },
        "sign_in_providers": [
            {
                "provider": p.provider,
                "label": p.label,
                "client_id": p.client_id,
                "tenant_id": p.tenant_id or "",
                "enabled": bool(p.enabled),
                "source": p.source,
            }
            for p in db.execute(select(AuthProvider).order_by(AuthProvider.id)).scalars()
        ],
        "company_directory": {
            "native_directory_enabled": admin_store.get_native_directory_enabled(),
            "clerk_instances": [
                {
                    "name": str(i["name"]),
                    "enabled": bool(i["enabled"]),
                    "production": str(i["secret_key"]).startswith("sk_live_"),
                    "configured_in_environment": bool(i.get("from_env")),
                }
                for i in admin_store.get_clerk_instances()
            ],
        },
        "storage_roots": {
            "nfs_root": nfs_root,
            "nfs_available": nfs_ok,
            "nfs_status": nfs_status,
            "linked_folder_root": link_root,
            "linked_folder_available": link_ok,
            "linked_folder_status": link_status,
        },
        "indexing_caps": {
            "values": indexing_caps.as_dict(),
            "defaults": indexing_caps.defaults_dict(),
            "bounds": indexing_caps.bounds_dict(),
        },
        # Company (cvk_) API keys of the active account's company.
        "company_api_keys": [
            {
                "name": k.name,
                "created_by": k.created_by,
                "created_at": _iso(k.created_at),
                "last_used_at": _iso(k.last_used_at),
            }
            for k in company_keys
        ],
        "assistant": {**load_policy().as_dict(), **deployment_configured(db)},
        "runtime": _runtime(),
    }


def admin_users(
    db: Session, scope: AdminScope, *, query: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """Accounts in the admin's scope (one row per account), optionally
    filtered by a case-insensitive substring of email / company / name."""
    supers = {e.lower() for e in get_settings().super_admin_list()}
    names_by_user = groups.group_names_by_user(db)
    owned = dict(
        db.execute(
            select(Folder.owner_id, func.count(Folder.id)).group_by(Folder.owner_id)
        ).all()
    )
    rows = filter_users_for_scope(
        scope, db.execute(select(User).order_by(User.email, User.company_id)).scalars().all()
    )
    needle = (query or "").strip().lower()
    if needle:
        rows = [
            u for u in rows
            if needle in u.email.lower()
            or needle in (u.company_name or "").lower()
            or needle in (u.display_name or "").lower()
        ]
    limit = max(1, min(limit, 500))
    return {
        "total": len(rows),
        "returned": min(len(rows), limit),
        "users": [
            {
                **_account(u),
                "display_name": u.display_name,
                "is_admin": bool(u.is_admin),
                "is_super_admin": u.email.lower() in supers,
                "native_allowed": admin_store.is_native_allowed(u.email),
                "groups": names_by_user.get(u.id, []),
                "folders_owned": owned.get(u.id, 0),
                "created_at": _iso(u.created_at),
            }
            for u in rows[:limit]
        ],
    }


def admin_groups(db: Session, scope: AdminScope, *, include_members: bool) -> list[dict[str, Any]]:
    """Voitta-native groups. Members are limited to users in the admin's
    scope (a regular admin never learns out-of-scope emails)."""
    out = []
    for g in groups.list_groups_with_counts(db):
        entry: dict[str, Any] = {
            "group_id": g["id"],
            "name": g["name"],
            "description": g["description"],
            "member_count": g["member_count"],
        }
        if include_members:
            members = db.execute(
                select(User)
                .join(UserGroup, UserGroup.user_id == User.id)
                .where(UserGroup.group_id == g["id"])
                .order_by(User.email)
            ).scalars()
            entry["members"] = [
                _account(u) for u in members if user_in_scope(scope, u)
            ]
        out.append(entry)
    return out


async def admin_company_directory(email: str) -> list[dict[str, Any]]:
    """Clerk users + organizations in the admin's scope (live)."""
    instances = await scoped_clerk_directory(email)
    for inst in instances:
        for u in inst["users"]:
            u.pop("image_url", None)
    return instances
