"""Microsoft sync sources (SharePoint + Teams) — shared auth + pickers.

Both connectors share the same OAuth client config (``ms_*`` columns) and
the same callback URL — only the saved row tells us afterwards which
source_type to land on. They register as two handlers with the SAME
``family`` so switching between them keeps the shared credentials.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Literal

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....db.models import FolderSyncSource
from ....services import events
from ....services.acl import CurrentUser
from ....services.sync import microsoft_auth as msa
from ....services.sync.sharepoint import (
    coerce_sites_field,
    encode_sites_field,
)
from ....services.sync.sharepoint import (
    list_all_sites as sp_list_all_sites,
)
from ....services.sync.teams import list_tenant_users as tm_list_tenant_users
from ...deps import current_user, db_session
from . import registry
from .base import check_owner, external_redirect_uri, oauth_router, router

if TYPE_CHECKING:
    from .core import SyncSourceIn

logger = logging.getLogger(__name__)


class SharePointSite(BaseModel):
    """One site row in the SharePoint picker."""

    id: str
    displayName: str = ""
    webUrl: str = ""


class SharePointSyncIn(BaseModel):
    """Payload when ``source_type == 'sharepoint'``."""

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    cert_pem: str = ""
    auth_method: Literal["", "oauth", "app_secret", "app_cert"] = ""
    sites: list[SharePointSite] = Field(default_factory=list)
    all_sites: bool = False
    use_loopback: bool = False


class TeamsSyncIn(BaseModel):
    """Payload when ``source_type == 'teams'``."""

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    cert_pem: str = ""
    auth_method: Literal["", "oauth", "app_secret", "app_cert"] = ""
    user_mode: Literal["me", "specific", "all_users"] = "me"
    user_id: str = ""
    include_attended: bool = True
    use_loopback: bool = False


class SharePointSyncOut(BaseModel):
    tenant_id: str
    client_id: str
    auth_method: str
    sites: list[SharePointSite]
    all_sites: bool
    has_client_secret: bool
    has_cert: bool
    use_loopback: bool
    connected: bool  # true once a refresh_token has been stored (oauth only)


class TeamsSyncOut(BaseModel):
    tenant_id: str
    client_id: str
    auth_method: str
    user_mode: str
    user_id: str
    include_attended: bool
    has_client_secret: bool
    has_cert: bool
    use_loopback: bool
    connected: bool


def clear_fields(src: FolderSyncSource) -> None:
    src.ms_tenant_id = None
    src.ms_client_id = None
    src.ms_client_secret = None
    src.ms_cert_pem = None
    src.ms_auth_method = None
    src.ms_refresh_token = None
    src.ms_use_loopback = False
    src.sp_selected_sites = None
    src.sp_all_sites = False
    src.tm_user_mode = None
    src.tm_user_id = None
    src.tm_include_attended = True


def build_sp_out(src: FolderSyncSource) -> SharePointSyncOut:
    return SharePointSyncOut(
        tenant_id=src.ms_tenant_id or "",
        client_id=src.ms_client_id or "",
        auth_method=src.ms_auth_method or "",
        sites=[SharePointSite(**s) for s in coerce_sites_field(src.sp_selected_sites)],
        all_sites=bool(src.sp_all_sites),
        has_client_secret=bool(src.ms_client_secret),
        has_cert=bool(src.ms_cert_pem),
        use_loopback=bool(src.ms_use_loopback),
        connected=bool(src.ms_refresh_token),
    )


def build_tm_out(src: FolderSyncSource) -> TeamsSyncOut:
    return TeamsSyncOut(
        tenant_id=src.ms_tenant_id or "",
        client_id=src.ms_client_id or "",
        auth_method=src.ms_auth_method or "",
        user_mode=src.tm_user_mode or "me",
        user_id=src.tm_user_id or "",
        include_attended=bool(src.tm_include_attended),
        has_client_secret=bool(src.ms_client_secret),
        has_cert=bool(src.ms_cert_pem),
        use_loopback=bool(src.ms_use_loopback),
        connected=bool(src.ms_refresh_token),
    )


def apply_config(
    *,
    body: SyncSourceIn,
    existing: FolderSyncSource | None,
    folder_id: int,
    **_: object,  # db/user context used only by credential-aware connectors
) -> FolderSyncSource:
    """Apply ``sharepoint`` / ``teams`` payload to a (new or existing) row.

    Shared auth fields land on ``ms_*``; per-connector specifics on
    ``sp_*`` / ``tm_*``. Switching from another source type clears the
    sibling credentials so we don't leak stale state across types.
    """
    is_sp = body.source_type == "sharepoint"
    cfg = body.sharepoint if is_sp else body.teams
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Missing '{body.source_type}' config",
        )
    if cfg.auth_method not in ("oauth", "app_secret", "app_cert"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick an auth method: 'oauth', 'app_secret', or 'app_cert'.",
        )
    if not cfg.tenant_id or not cfg.client_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "tenant_id and client_id are required.",
        )

    existing_has_secret = bool(existing and existing.ms_client_secret)
    existing_has_cert = bool(existing and existing.ms_cert_pem)
    if cfg.auth_method == "app_secret" and not (
        cfg.client_secret or existing_has_secret
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Auth method 'app_secret' needs a client_secret.",
        )
    if cfg.auth_method == "app_cert" and not (
        cfg.cert_pem or existing_has_cert
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Auth method 'app_cert' needs a PEM private key + certificate.",
        )
    if cfg.auth_method == "oauth" and not (
        cfg.client_secret or existing_has_secret
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Delegated OAuth needs a client_secret for token refresh.",
        )

    src = existing or FolderSyncSource(
        folder_id=folder_id, source_type=body.source_type
    )
    if existing is not None and existing.source_type != body.source_type:
        # sharepoint ↔ teams keep the shared ms_* credentials (same family);
        # arriving from any other type wipes everything else.
        registry.clear_other_sources(src, body.source_type)
    src.source_type = body.source_type

    # Shared MS fields.
    src.ms_tenant_id = cfg.tenant_id.strip()
    src.ms_client_id = cfg.client_id.strip()
    if cfg.client_secret:
        src.ms_client_secret = cfg.client_secret
    if cfg.cert_pem:
        src.ms_cert_pem = cfg.cert_pem
    prev_method = src.ms_auth_method
    src.ms_auth_method = cfg.auth_method
    # Auth-method changes invalidate the stored refresh_token (it was
    # issued under different scopes / client config).
    if prev_method and prev_method != cfg.auth_method:
        src.ms_refresh_token = None
    prev_loopback = bool(existing and existing.ms_use_loopback)
    if prev_loopback != bool(cfg.use_loopback):
        # Same story as gdrive — refresh tokens are bound to the
        # redirect URI used at consent. Flipping loopback rotates that
        # URI, so the existing token is useless.
        src.ms_refresh_token = None
    src.ms_use_loopback = bool(cfg.use_loopback)

    if is_sp:
        src.sp_selected_sites = encode_sites_field(
            [{"id": s.id, "displayName": s.displayName, "webUrl": s.webUrl}
             for s in cfg.sites]
        )
        src.sp_all_sites = bool(cfg.all_sites)
        # Clear teams-specific fields in case we're switching from teams.
        src.tm_user_mode = None
        src.tm_user_id = None
    else:
        src.tm_user_mode = cfg.user_mode
        src.tm_user_id = cfg.user_id.strip() or None
        src.tm_include_attended = bool(cfg.include_attended)
        src.sp_selected_sites = None
        src.sp_all_sites = False

    return src


def _sp_trigger_check(src: FolderSyncSource) -> None:
    if not (src.sp_all_sites or coerce_sites_field(src.sp_selected_sites)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick at least one SharePoint site (or enable 'all sites').",
        )


def _tm_trigger_check(src: FolderSyncSource) -> None:
    mode = src.tm_user_mode or "me"
    if mode == "specific" and not src.tm_user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick a user for Teams 'specific user' mode before syncing.",
        )


registry.register(
    registry.SourceHandler(
        source_type="sharepoint",
        out_field="sharepoint",
        family="microsoft",
        apply=apply_config,
        build_out=build_sp_out,
        clear=clear_fields,
        trigger_check=_sp_trigger_check,
    )
)
registry.register(
    registry.SourceHandler(
        source_type="teams",
        out_field="teams",
        family="microsoft",
        apply=apply_config,
        build_out=build_tm_out,
        clear=clear_fields,
        trigger_check=_tm_trigger_check,
    )
)


# ---------------------------------------------------------------------------
# OAuth init / callback + pickers + scope check
# ---------------------------------------------------------------------------


class MsAuthInitOut(BaseModel):
    auth_url: str


# Same loopback story as Google: the admin can register only a localhost
# URL in Azure AD and run a small nginx bridge that forwards back here.
MS_LOOPBACK_PORT = 53682
MS_LOOPBACK_REDIRECT_URI = (
    f"http://localhost:{MS_LOOPBACK_PORT}/api/sync/oauth/microsoft/callback"
)


def _ms_redirect_uri(request: Request, *, use_loopback: bool = False) -> str:
    return external_redirect_uri(
        request,
        "/api/sync/oauth/microsoft/callback",
        loopback_uri=MS_LOOPBACK_REDIRECT_URI if use_loopback else None,
    )


def _build_ms_auth_for_row(src: FolderSyncSource) -> msa.MicrosoftAuth:
    return msa.MicrosoftAuth(
        tenant_id=src.ms_tenant_id or "",
        client_id=src.ms_client_id or "",
        client_secret=src.ms_client_secret or "",
        cert_pem=src.ms_cert_pem or "",
        refresh_token=src.ms_refresh_token or "",
        method=src.ms_auth_method or "",
    )


@router.post("/microsoft/auth", response_model=MsAuthInitOut)
def ms_auth_init(
    folder_id: int,
    request: Request,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsAuthInitOut:
    """Build the Microsoft OAuth URL the UI should pop open."""
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Configure SharePoint or Teams (tenant/client_id/secret) before connecting",
        )
    if not (src.ms_tenant_id and src.ms_client_id and src.ms_client_secret):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Save tenant_id, client_id and client_secret before connecting.",
        )
    state = base64.urlsafe_b64encode(str(folder_id).encode()).decode()
    auth_url = msa.get_auth_url(
        tenant_id=src.ms_tenant_id,
        client_id=src.ms_client_id,
        redirect_uri=_ms_redirect_uri(request, use_loopback=bool(src.ms_use_loopback)),
        state=state,
    )
    return MsAuthInitOut(auth_url=auth_url)


@oauth_router.get("/oauth/microsoft/callback")
async def ms_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
    admin_consent: str | None = Query(None),
    tenant: str | None = Query(None),
) -> HTMLResponse:
    """Microsoft OAuth callback — works for both SharePoint and Teams rows.

    Microsoft can redirect back with ``?error=...&error_description=...``
    instead of ``?code=...`` (admin-consent missing, scope typo,
    redirect-uri mismatch). We surface that verbatim so the admin can
    fix the real issue instead of staring at a 422 schema error from
    a missing ``code`` query param.
    """
    # Admin-consent return — the /adminconsent endpoint redirects here
    # with ``admin_consent=True&tenant=…`` and no ``code`` or ``state``.
    # Tenant-wide grant is already saved server-side at Azure AD by the
    # time the redirect fires; nothing for us to do here except show a
    # friendly page so the admin doesn't think it failed.
    if (admin_consent or "").lower() == "true":
        logger.info("Microsoft admin consent recorded for tenant=%s", tenant)
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:20px;'>"
            "<h3>Admin consent recorded ✓</h3>"
            "<p>Tenant-wide permissions are now granted. You can close "
            "this tab — the user who was setting up the integration can "
            "click Connect again and sign in normally.</p>"
            "</body></html>"
        )
    if error:
        logger.warning(
            "Microsoft OAuth callback returned error=%s desc=%s",
            error, error_description,
        )
        body = (
            f"<html><body style='font-family:sans-serif;padding:20px;'>"
            f"<h3>Microsoft sign-in failed</h3>"
            f"<p><strong>{error}</strong></p>"
            f"<pre style='white-space:pre-wrap;background:#f4f4f4;padding:12px;'>"
            f"{(error_description or '').strip()}</pre>"
            f"<p>Close this tab and adjust the Azure AD app, then try again.</p>"
            f"</body></html>"
        )
        return HTMLResponse(body, status_code=400)
    if not code:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Microsoft callback did not include code or error",
        )
    if not state:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Microsoft callback did not include state",
        )
    try:
        folder_id = int(base64.urlsafe_b64decode(state.encode()).decode())
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid state parameter"
        ) from e

    from ....db.database import session_scope

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        if src is None or src.source_type not in ("sharepoint", "teams"):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Microsoft source not found for this state",
            )
        tenant_id = src.ms_tenant_id or ""
        client_id = src.ms_client_id or ""
        client_secret = src.ms_client_secret or ""
        use_loopback = bool(src.ms_use_loopback)
        source_type = src.source_type

    try:
        tokens = await msa.exchange_code_for_tokens(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=_ms_redirect_uri(request, use_loopback=use_loopback),
        )
    except Exception as e:
        logger.exception("Microsoft OAuth callback failed for folder %s", folder_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Microsoft did not return a refresh_token. Make sure 'offline_access' "
            "is in the requested scopes and try again.",
        )

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        if src is not None:
            src.ms_refresh_token = refresh_token

    events.publish(
        "folders",
        {
            "type": "folder.ms_connected",
            "folder_id": folder_id,
            "source_type": source_type,
        },
    )

    return HTMLResponse(
        "<html><body><script>window.close()</script>"
        "<p>Microsoft account connected. You can close this tab.</p></body></html>"
    )


class MsSitesOut(BaseModel):
    sites: list[SharePointSite]


@router.get("/microsoft/sites", response_model=MsSitesOut)
async def ms_list_sites(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsSitesOut:
    """List sites the credentials can see (for the picker)."""
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Microsoft source")
    auth = _build_ms_auth_for_row(src)
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect (or configure app-only creds) before listing sites.",
        )
    try:
        sites = await sp_list_all_sites(auth)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    # Persist the rotated refresh token, if any.
    if auth.rotated_refresh_token:
        src.ms_refresh_token = auth.rotated_refresh_token
        db.commit()
    return MsSitesOut(sites=[SharePointSite(**s) for s in sites])


class MsUser(BaseModel):
    id: str
    displayName: str = ""
    userPrincipalName: str = ""
    mail: str = ""


class MsUsersOut(BaseModel):
    users: list[MsUser]


@router.get("/microsoft/users", response_model=MsUsersOut)
async def ms_list_users(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsUsersOut:
    """Tenant user picker for Teams 'specific user' mode."""
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Microsoft source")
    auth = _build_ms_auth_for_row(src)
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect (or configure app-only creds) before listing users.",
        )
    try:
        users = await tm_list_tenant_users(auth)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if auth.rotated_refresh_token:
        src.ms_refresh_token = auth.rotated_refresh_token
        db.commit()
    return MsUsersOut(users=[MsUser(**u) for u in users])


class MsScopeMissing(BaseModel):
    feature: str
    scope: str
    impact: str


class MsScopeCheckOut(BaseModel):
    granted: list[str]
    missing: list[MsScopeMissing]
    app_only: bool


@router.get("/microsoft/scope-check", response_model=MsScopeCheckOut)
async def ms_scope_check(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsScopeCheckOut:
    """Mint a fresh token and compare its claims against required scopes.

    Drives the yellow "missing scope" callout on the sync config page.
    """
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Microsoft source")
    auth = _build_ms_auth_for_row(src)
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect (or configure app-only creds) before checking scopes.",
        )
    app_only = auth.method in ("app_secret", "app_cert")
    try:
        if app_only:
            token = await msa.get_app_only_token(auth)
        else:
            token = await msa.refresh_access_token(auth)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if auth.rotated_refresh_token:
        src.ms_refresh_token = auth.rotated_refresh_token
        db.commit()
    report = msa.compute_missing_scopes(token, app_only=app_only)
    return MsScopeCheckOut(
        granted=report["granted"],
        missing=[MsScopeMissing(**m) for m in report["missing"]],
        app_only=app_only,
    )
