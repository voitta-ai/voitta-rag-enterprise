"""Sign-in routes — currently Google OAuth only.

The session is a Starlette signed cookie ('voitta_session') keyed off the
authenticated email. ``current_user`` (api/deps.py) reads it and falls
through to the existing header / single-user / dev-user paths when the
cookie is absent, so MCP clients and proxy-auth deployments keep working
without configuring Google auth at all.

The OAuth callback URL is registered once with the Google client and is
not folder-scoped, mirroring the Drive-sync OAuth flow:
``/api/auth/google/callback``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import ApiKey
from ...services.acl import CurrentUser, get_or_create_user
from ...services.admin_store import is_email_allowed, is_super_admin
from ..deps import current_user, db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_LOGIN_SCOPES = "openid email profile"


def _redirect_uri(request: Request) -> str:
    """Build the callback URL the way Google's consent screen will see it.

    Mirrors the Drive-sync helper: read ``X-Forwarded-Proto`` /
    ``X-Forwarded-Host`` so the URL matches what the user actually hit even
    when a reverse proxy terminated TLS in front of the app.
    """
    fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    fwd_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    proto = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{proto}://{host}/api/auth/google/callback"


# ---------------------------------------------------------------------------
# Public config — UI calls this to decide whether to render the Sign-in
# button and where to send the browser. Does not require auth.
# ---------------------------------------------------------------------------


class AuthConfigOut(BaseModel):
    google_enabled: bool


@router.get("/config", response_model=AuthConfigOut)
def auth_config() -> AuthConfigOut:
    s = get_settings()
    return AuthConfigOut(google_enabled=s.google_auth_enabled)


# ---------------------------------------------------------------------------
# /api/auth/me — used by the UI bootstrap to decide login-vs-app screen.
# Intentionally returns 401 when there's no session, instead of provoking
# the dev-user / single-user fallbacks; the UI then knows to show login.
# ---------------------------------------------------------------------------


class MeOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    # ``is_admin`` reflects the *real* signed-in user — admin status is
    # never inherited via impersonation. The SPA uses this to decide
    # whether to render the Admin button.
    is_admin: bool
    # When the admin has chosen "view as <other>", these surface the
    # effective identity. UI shows a banner + a "Stop impersonating"
    # button. ``acting_as_user_id is None`` means no impersonation in
    # progress and the SPA renders the user's own data.
    acting_as_user_id: int | None
    acting_as_email: str | None


@router.get("/me", response_model=MeOut)
def me(
    request: Request,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MeOut:
    """Return the effective signed-in user (impersonation honoured).

    Admin-related fields reflect the real (pre-impersonation) identity.
    A 401 from ``current_user`` lets the SPA render its login gate.
    """
    from ...db.models import User as _User

    eff_row = db.get(_User, user.id)

    # Pull the real identity from the session cookie directly so the
    # ``is_admin`` flag isn't masked by impersonation. Fall back to dev
    # shortcuts the way ``resolve_user_email`` does.
    real_email = request.session.get("user_email") if hasattr(request, "session") else None
    s = get_settings()
    if s.single_user:
        real_email = "root@localhost"
    elif s.dev_user:
        real_email = s.dev_user
    real_row = (
        db.query(_User).filter(_User.email == real_email).one_or_none()
        if real_email
        else None
    )
    is_admin = bool(real_row.is_admin) if real_row else False

    acting_id = (
        request.session.get("acting_as_user_id")
        if hasattr(request, "session")
        else None
    )
    acting_email: str | None = None
    if acting_id is not None and is_admin and eff_row is not None and eff_row.id != (
        real_row.id if real_row else -1
    ):
        acting_email = eff_row.email
    else:
        acting_id = None

    return MeOut(
        id=user.id,
        email=user.email,
        display_name=eff_row.display_name if eff_row else None,
        is_admin=is_admin,
        acting_as_user_id=int(acting_id) if acting_id is not None else None,
        acting_as_email=acting_email,
    )


# ---------------------------------------------------------------------------
# Google OAuth login — start
# ---------------------------------------------------------------------------


@router.get("/login/google")
def google_login_start(request: Request) -> RedirectResponse:
    """Build the Google consent URL and redirect the browser.

    A short-lived ``state`` is stashed in the session and verified in the
    callback to prevent CSRF.
    """
    s = get_settings()
    if not s.google_auth_enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Google login is not configured"
        )
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = {
        "client_id": s.google_auth_client_id,
        "response_type": "code",
        "redirect_uri": _redirect_uri(request),
        "scope": GOOGLE_LOGIN_SCOPES,
        "state": state,
        # ``select_account`` lets the user switch between Google accounts
        # rather than auto-resuming the last session.
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}")


# ---------------------------------------------------------------------------
# Google OAuth login — callback
# ---------------------------------------------------------------------------


@router.get("/google/callback")
async def google_login_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(""),
    db: Session = Depends(db_session),
) -> Any:
    s = get_settings()
    if not s.google_auth_enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Google login is not configured"
        )

    expected_state = request.session.get("oauth_state")
    if not expected_state or state != expected_state:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid OAuth state — start the login again"
        )
    request.session.pop("oauth_state", None)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": s.google_auth_client_id,
                    "client_secret": s.google_auth_client_secret,
                    "code": code,
                    "redirect_uri": _redirect_uri(request),
                    "grant_type": "authorization_code",
                },
            )
            if tok_resp.status_code != 200:
                _raise_google_error("token exchange", tok_resp)
            access_token = tok_resp.json()["access_token"]
            ui_resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if ui_resp.status_code != 200:
                _raise_google_error("userinfo", ui_resp)
            profile = ui_resp.json()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Google login failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Google login failed: {e}") from e

    email = (profile.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Google profile did not include an email"
        )
    if not profile.get("email_verified", True):
        # Reject unverified addresses — anyone could attach the email to a
        # fake Google account otherwise.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google reports this email is not verified",
        )

    if not is_email_allowed(email):
        logger.warning("login_denied: %s", email)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is not authorized for this deployment. "
            "Contact your administrator.",
        )

    display_name = profile.get("name") or email.split("@")[0]
    user = get_or_create_user(db, email)
    if not user.display_name and display_name:
        user.display_name = display_name
    # Bootstrap admins are stamped on every sign-in: even if a previous
    # admin flipped the flag off in the DB, the next sign-in re-grants it.
    # That makes the env var the recoverable source of truth — wipe it
    # to demote, redeploy to promote.
    if is_super_admin(email):
        user.is_admin = True
    db.commit()

    request.session["user_email"] = email
    logger.info("login: %s (id=%d)", email, user.id)

    # Land back on the SPA. Use 303 so the browser switches to GET.
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


def _raise_google_error(op: str, resp: httpx.Response) -> None:
    try:
        body = resp.json()
        err = body.get("error_description") or body.get("error") or ""
    except Exception:
        err = resp.text[:300]
    logger.error("Google %s failed (%d): %s", op, resp.status_code, err)
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY, f"Google {op} failed: {err}"
    )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    """Drop the session cookie. Header-based / dev-user logins ignore this."""
    request.session.clear()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Tiny "popup-closes-itself" landing — currently unused, kept for parity
# with the Drive-sync OAuth popup pattern in case we ever switch login to
# a popup flow.
# ---------------------------------------------------------------------------


def _self_closing_html(message: str) -> HTMLResponse:
    return HTMLResponse(
        "<html><body><script>window.close()</script>"
        f"<p>{message}</p></body></html>"
    )


# ---------------------------------------------------------------------------
# Personal API keys
#
# Token format: ``vk_<43 url-safe base64 chars>`` → ~256 bits of entropy.
# The full token is shown to the user exactly once at creation time. We
# persist only its SHA-256 hash plus a short prefix for UI display.
# ---------------------------------------------------------------------------

KEY_TOKEN_PREFIX = "vk_"
KEY_DISPLAY_PREFIX_CHARS = 10  # "vk_" + 7 random chars — enough to disambiguate
MAX_KEY_NAME_LEN = 80


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token() -> tuple[str, str, str]:
    """Generate a fresh API key.

    Returns ``(token, prefix, key_hash)``. ``token`` is the only ever
    plaintext copy — call sites must surface it to the user immediately
    and must never log it.
    """
    body = secrets.token_urlsafe(32)
    token = f"{KEY_TOKEN_PREFIX}{body}"
    prefix = token[:KEY_DISPLAY_PREFIX_CHARS]
    return token, prefix, _hash_token(token)


def verify_token(db: Session, token: str) -> ApiKey | None:
    """Look up an API key by its plaintext token; bump ``last_used_at``.

    Returns the ``ApiKey`` row when the token matches, ``None`` otherwise.
    Exposed at module level so the upcoming MCP auth flow can import it
    without depending on this whole router. The caller commits.
    """
    if not token or not token.startswith(KEY_TOKEN_PREFIX):
        return None
    key = db.execute(
        select(ApiKey).where(ApiKey.key_hash == _hash_token(token))
    ).scalar_one_or_none()
    if key is None:
        return None
    key.last_used_at = int(time.time())
    return key


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    created_at: int
    last_used_at: int | None


class ApiKeyCreatedOut(ApiKeyOut):
    """Returned on POST — includes the plaintext ``token`` exactly once."""

    token: str


class ApiKeyCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_KEY_NAME_LEN)


def _to_out(k: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        name=k.name,
        prefix=k.prefix,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
    )


@router.get("/keys", response_model=list[ApiKeyOut])
def list_keys(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[ApiKeyOut]:
    """Return the signed-in user's keys, newest first."""
    rows = (
        db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user.id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        )
        .scalars()
        .all()
    )
    return [_to_out(k) for k in rows]


@router.post("/keys", response_model=ApiKeyCreatedOut)
def create_key(
    body: ApiKeyCreateIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> ApiKeyCreatedOut:
    """Mint a new key. The plaintext ``token`` is in the response and will
    never be returned again — the UI must show a "copy now" callout.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Key name cannot be blank"
        )
    token, prefix, key_hash = mint_token()
    row = ApiKey(
        user_id=user.id,
        name=name,
        prefix=prefix,
        key_hash=key_hash,
        created_at=int(time.time()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("api_key.create user=%s id=%d name=%r", user.email, row.id, name)
    return ApiKeyCreatedOut(**_to_out(row).model_dump(), token=token)


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_key(
    key_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    row = db.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        # Same response for "not yours" and "not found" — don't leak
        # whether a key id belongs to a different user.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    db.delete(row)
    db.commit()
    logger.info("api_key.delete user=%s id=%d", user.email, key_id)
