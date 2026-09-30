"""Cross-site WebSocket hijacking guard.

The WebSockets authenticate from the session cookie. ``SameSite=Lax`` keeps
that cookie off handshakes from other *sites*, but "site" is the registrable
domain: a page on any sibling subdomain is same-site and gets the cookie
attached (and browsers without SameSite support attach it everywhere). A WS
handshake is also not subject to CORS. Without a check such a page could
open a socket as the signed-in user — for the assistant that means reading
its answers and spending the deployment's LLM credit.

Browsers always send ``Origin`` on a WS handshake and a page cannot forge
it, so the rule is: when ``Origin`` is present it must be this server's own
origin (the scheme-less ``Host`` the request arrived on), the configured
``VOITTA_PUBLIC_BASE_URL``, or one of ``VOITTA_WS_ALLOWED_ORIGINS``.
A handshake without ``Origin`` comes from a non-browser client, which
cannot be carrying a victim's cookie, and is allowed.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.websockets import WebSocket

from ..config import get_settings

# Application close code for a refused origin (4000-4999 is private use).
WS_CLOSE_FORBIDDEN_ORIGIN = 4403


def _netloc(url: str) -> str:
    return urlsplit(url).netloc.lower()


def websocket_origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return True
    origin_netloc = _netloc(origin)
    if not origin_netloc:
        return False
    host = (ws.headers.get("host") or "").lower()
    if host and origin_netloc == host:
        return True
    settings = get_settings()
    allowed = settings.ws_allowed_origin_list()
    if settings.public_base_url:
        allowed.append(settings.public_base_url.rstrip("/").lower())
    return origin.rstrip("/").lower() in allowed
