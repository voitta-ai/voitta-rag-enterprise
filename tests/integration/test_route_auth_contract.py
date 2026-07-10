"""Route-level auth-contract test — every route's access pattern, pinned.

Complements test_route_surface.py (which pins WHICH routes exist) by
pinning HOW each route authenticates. Every (method, path) is classified:

- ``public``      — no identity dep; must never 401 (bad input may 4xx).
- ``bearer``      — identity-requiring and API-key reachable: 401 without
                    credentials, works with vk_ and cvk_ alike.
- ``cookie_only`` — identity-requiring but excluded from API keys
                    (admin console, key management, account switch):
                    401 without credentials, 403 with a VALID key.

The completeness check forces every future route to declare its class in
the same commit that adds it — an unclassified route fails loudly instead
of silently landing in the wrong bucket. The behavior passes then verify
the classes against the real app with no dependency overrides: the same
requests a curl client would send.
"""

from __future__ import annotations

import re
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.services import admin_store

PUBLIC = {
    "GET /",
    "GET /favicon.ico",
    "GET /healthz",
    "GET /api/health",
    "GET /api/auth/config",
    "GET /api/auth/google/callback",
    "GET /api/auth/login/google",
    "POST /api/auth/logout",
    "GET /api/sync/oauth/google/callback",
    "GET /api/sync/oauth/microsoft/callback",
}

COOKIE_ONLY = {
    "DELETE /api/admin/allowlist/domains/{domain}",
    "DELETE /api/admin/allowlist/users/{email}",
    "DELETE /api/admin/auth-providers/{provider_id}",
    "DELETE /api/admin/blocklist/{email}",
    "DELETE /api/admin/groups/{group_id}",
    "DELETE /api/admin/groups/{group_id}/members/{user_id}",
    "DELETE /api/admin/impersonate",
    "DELETE /api/admin/users/{user_id}",
    "GET /api/admin/allowlist",
    "GET /api/admin/auth-providers",
    "GET /api/admin/clerk/directory",
    "GET /api/admin/groups",
    "GET /api/admin/indexing-caps",
    "GET /api/admin/settings",
    "GET /api/admin/users",
    "PATCH /api/admin/auth-providers/{provider_id}",
    "PATCH /api/admin/groups/{group_id}",
    "PATCH /api/admin/indexing-caps",
    "PATCH /api/admin/settings",
    "PATCH /api/admin/users/{user_id}",
    "POST /api/admin/allowlist/domains",
    "POST /api/admin/allowlist/users",
    "POST /api/admin/auth-providers",
    "POST /api/admin/auth-providers/{provider_id}/check",
    "POST /api/admin/blocklist",
    "POST /api/admin/clerk/impersonate",
    "POST /api/admin/groups",
    "POST /api/admin/groups/{group_id}/members",
    "POST /api/admin/impersonate/{user_id}",
    "POST /api/admin/users",
    "DELETE /api/auth/company-keys/{key_id}",
    "GET /api/auth/company-keys",
    "POST /api/auth/company-keys",
    "DELETE /api/auth/keys/{key_id}",
    "GET /api/auth/keys",
    "POST /api/auth/keys",
    "POST /api/auth/account/{account_id}",
}

BEARER = {
    "GET /api/auth/me",  # the whoami exception
    "GET /api/docs",
    "GET /api/openapi.json",
    "DELETE /api/folders/{folder_id}",
    "DELETE /api/folders/{folder_id}/dirs",
    "DELETE /api/folders/{folder_id}/files/{file_id}",
    "DELETE /api/folders/{folder_id}/sync",
    "DELETE /api/folders/{folder_id}/sync/error",
    "GET /api/folders",
    "GET /api/folders/active-ids",
    "GET /api/folders/root",
    "GET /api/folders/{folder_id}/dirs",
    "GET /api/folders/{folder_id}/files",
    "GET /api/folders/{folder_id}/stats",
    "GET /api/folders/{folder_id}/sync",
    "GET /api/folders/{folder_id}/sync/confluence/spaces",
    "GET /api/folders/{folder_id}/sync/google-drive/api-status",
    "GET /api/folders/{folder_id}/sync/google-drive/browse",
    "GET /api/folders/{folder_id}/sync/google-drive/folders",
    "GET /api/folders/{folder_id}/sync/jira/projects",
    "GET /api/folders/{folder_id}/sync/microsoft/scope-check",
    "GET /api/folders/{folder_id}/sync/microsoft/sites",
    "GET /api/folders/{folder_id}/sync/microsoft/users",
    "PATCH /api/folders/{folder_id}/active",
    "PATCH /api/folders/{folder_id}/rename",
    "PATCH /api/folders/{folder_id}/share",
    "POST /api/folders",
    "POST /api/folders/{folder_id}/grant",
    "POST /api/folders/{folder_id}/mkdir",
    "POST /api/folders/{folder_id}/reindex",
    "POST /api/folders/{folder_id}/revoke",
    "POST /api/folders/{folder_id}/sync/branches",
    "POST /api/folders/{folder_id}/sync/google-drive/auth",
    "POST /api/folders/{folder_id}/sync/microsoft/auth",
    "POST /api/folders/{folder_id}/sync/trigger",
    "POST /api/folders/{folder_id}/upload",
    "PUT /api/folders/{folder_id}/sync",
    "GET /api/files/{file_id}",
    "GET /api/files/{file_id}/email",
    "GET /api/files/{file_id}/images",
    "GET /api/files/{file_id}/layout",
    "GET /api/files/{file_id}/page-images",
    "GET /api/files/{file_id}/raw",
    "GET /api/files/{file_id}/stl",
    "GET /api/files/{file_id}/text",
    "GET /api/images/{image_id}",
    "DELETE /api/jobs/cleanup-failed",
    "GET /api/jobs/recent",
    "POST /api/jobs/cancel-all",
    "POST /api/jobs/retry-failed",
    "POST /api/jobs/{job_id}/cancel",
    "POST /api/jobs/{job_id}/retry",
    "POST /api/search",
    "GET /api/sync/local/accounts",
    "GET /api/sync/local/browse",
    "GET /api/sync/nfs/browse",
    "GET /api/sync/nfs/status",
    "POST /api/sync/local/connect",
    "GET /api/users",
    "GET /api/users/me",
    "POST /api/users",
    # MCP splice: bearer-authed by BearerAuthMiddleware rather than the
    # deps seam, but the caller-visible contract is the same class.
    "DELETE /mcp",
    "POST /mcp",
}

# Signed-URL routes carry their own credential (an HMAC token in the
# path); a bad token 401s on its own terms. The contract here is that
# identity auth is IRRELEVANT: the response must be byte-for-byte the
# same with no auth, a vk_ key, or a cvk_ pair.
SIGNED_URL = {"GET /api/assets/{token}"}

COOKIE_ONLY_DETAIL = "session-cookie only"

_PARAM_VALUES = {"token": "not-a-real-token", "domain": "example.com", "email": "x@example.com"}


def _fill(path: str) -> str:
    return re.sub(
        r"\{(\w+)\}", lambda m: _PARAM_VALUES.get(m.group(1), "1"), path
    )


def _all_routes(app: FastAPI) -> set[str]:
    return {
        f"{m} {r.path}"
        for r in app.routes
        if hasattr(r, "methods")
        for m in r.methods
        if m != "HEAD"
    }


def _seed_keys() -> tuple[str, str]:
    """Mint one vk_ (alice) and one native-space cvk_ directly; allowlist
    the cvk_ acting member."""
    from voitta_rag_enterprise.api.routes.api_keys import mint_token
    from voitta_rag_enterprise.api.routes.company_keys import mint_company_token
    from voitta_rag_enterprise.db.models import ApiKey, CompanyApiKey
    from voitta_rag_enterprise.services.acl import get_or_create_user

    vk, vp, vh = mint_token()
    cvk, cp, ch = mint_company_token()
    with session_scope() as s:
        alice = get_or_create_user(s, "alice@x")
        s.add(
            ApiKey(
                user_id=alice.id, name="contract", prefix=vp, key_hash=vh,
                created_at=int(time.time()),
            )
        )
        s.add(
            CompanyApiKey(
                company_id="", name="contract", prefix=cp, key_hash=ch,
                created_by="admin@x", created_at=int(time.time()),
            )
        )
    admin_store.add_allowed_user("member@x")
    return vk, cvk


def test_every_route_is_classified(env: None) -> None:
    """Completeness: adding a route without declaring its auth class fails."""
    from voitta_rag_enterprise.main import create_app

    actual = _all_routes(create_app())
    classes = [PUBLIC, COOKIE_ONLY, BEARER, SIGNED_URL]
    classified = set().union(*classes)
    overlap = {
        r
        for i, a in enumerate(classes)
        for b in classes[i + 1 :]
        for r in a & b
    }
    assert not overlap, f"Routes in more than one class: {sorted(overlap)}"
    unclassified = sorted(actual - classified)
    stale = sorted(classified - actual)
    assert not unclassified and not stale, (
        f"Auth contract out of date.\n"
        f"Unclassified (new route? declare its class): {unclassified}\n"
        f"Stale (route removed/renamed): {stale}"
    )


def test_access_patterns_match_declared_classes(env: None) -> None:
    """Drive every route three ways — no auth, vk_, cvk_+email — and assert
    the observed auth behavior matches the declared class. No dependency
    overrides: these are the same requests a curl client sends."""
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    vk, cvk = _seed_keys()
    headers_by_pass = {
        "no-auth": {},
        "vk": {"Authorization": f"Bearer {vk}"},
        "cvk": {
            "Authorization": f"Bearer {cvk}",
            "X-Voitta-User-Email": "member@x",
        },
    }

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # raise_server_exceptions=False: dummy ids can 500 deep in handlers
    # (e.g. search with no Qdrant collections); only auth statuses matter.
    with TestClient(app, raise_server_exceptions=False) as client:
        for route in sorted(PUBLIC | COOKIE_ONLY | BEARER | SIGNED_URL):
            method, path = route.split(" ", 1)
            url = _fill(path)
            signed_url_responses: list[tuple[int, bytes]] = []
            for pass_name, headers in headers_by_pass.items():
                r = client.request(method, url, headers=headers)
                detail = ""
                try:
                    body = r.json()
                    if isinstance(body, dict):
                        detail = str(body.get("detail", ""))
                except Exception:
                    pass
                tag = f"[{pass_name}] {route} -> {r.status_code} {detail[:60]!r}"

                if route in SIGNED_URL:
                    # Identity auth must be irrelevant: identical response
                    # whether the request carries no key, vk_, or cvk_.
                    signed_url_responses.append((r.status_code, r.content))
                    check(
                        COOKIE_ONLY_DETAIL not in detail,
                        f"signed-url route hit the bearer guard: {tag}",
                    )
                elif route in PUBLIC:
                    # Public routes never demand credentials — with or
                    # without a key (which they must simply ignore).
                    check(r.status_code != 401, f"public route 401'd: {tag}")
                    check(
                        COOKIE_ONLY_DETAIL not in detail,
                        f"public route hit the bearer guard: {tag}",
                    )
                elif pass_name == "no-auth":
                    # Identity-requiring routes demand credentials.
                    check(
                        r.status_code == 401,
                        f"unauthenticated request not rejected: {tag}",
                    )
                elif route in COOKIE_ONLY:
                    check(
                        r.status_code == 403 and COOKIE_ONLY_DETAIL in detail,
                        f"cookie-only route reachable with an API key: {tag}",
                    )
                else:  # BEARER with a valid key
                    check(r.status_code != 401, f"valid key rejected: {tag}")
                    check(
                        COOKIE_ONLY_DETAIL not in detail,
                        f"bearer route hit the cookie-only guard: {tag}",
                    )

            if route in SIGNED_URL:
                check(
                    len(set(signed_url_responses)) == 1,
                    f"signed-url route varies with auth: {route} -> "
                    f"{[s for s, _ in signed_url_responses]}",
                )

    assert not failures, "Auth contract violations:\n" + "\n".join(failures)
