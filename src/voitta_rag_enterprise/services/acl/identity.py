"""Request identity — who is calling, before any account/authorization logic.

``CurrentUser`` is the request-scoped identity contract every route handler
receives; ``resolve_user_email`` turns the transport-level signal (env
shortcut or session cookie) into an email. Everything downstream (accounts,
folder ACL) keys off these.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status

from ...config import get_settings

ROOT_EMAIL = "root@localhost"


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    # Active account's company scope. '' = the Personal account.
    company_id: str = ""
    company_name: str = ""


def resolve_user_email(session_email: str | None = None) -> str:
    """Pick the email for this request.

    Priority — first match wins:

    1. ``VOITTA_SINGLE_USER`` → ``root@localhost`` (local dev)
    2. ``VOITTA_DEV_USER`` → that email (local dev)
    3. ``session_email`` (signed cookie set by Google login)
    4. raise 401

    Self-asserted headers (``X-Forwarded-Email``, ``X-User-Name``) used to be
    accepted; they're not anymore. Web requests authenticate via the session
    cookie, MCP requests via ``Authorization: Bearer`` (handled in
    ``mcp_server.BearerAuthMiddleware``, not here).
    """
    s = get_settings()
    if s.single_user:
        return ROOT_EMAIL
    if s.dev_user:
        return s.dev_user
    if not session_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in. Sign in with Google, or set VOITTA_DEV_USER for local dev.",
        )
    return session_email
