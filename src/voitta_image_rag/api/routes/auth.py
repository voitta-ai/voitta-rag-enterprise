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

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...config import get_settings
from ...services.acl import get_or_create_user
from ..deps import db_session

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


@router.get("/me", response_model=MeOut)
def me(
    request: Request,
    db: Session = Depends(db_session),
) -> MeOut:
    s = get_settings()
    email = None
    if s.single_user:
        from ...services.acl import ROOT_EMAIL

        email = ROOT_EMAIL
    elif s.dev_user:
        email = s.dev_user
    else:
        email = request.session.get("user_email")
        if not email:
            email = request.headers.get("x-forwarded-email") or request.headers.get(
                "x-user-name"
            )
    if not email:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    user = get_or_create_user(db, email)
    db.commit()
    return MeOut(id=user.id, email=user.email, display_name=user.display_name)


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

    display_name = profile.get("name") or email.split("@")[0]
    user = get_or_create_user(db, email)
    if not user.display_name and display_name:
        user.display_name = display_name
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
