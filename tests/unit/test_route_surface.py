"""Route-table snapshot — the API surface contract.

Refactors that split route modules into packages (sync, admin, auth/api-keys)
must never add, drop, or rename an HTTP route. This test freezes the full
(method, path) surface so any accidental change fails with a readable diff.

When a route change is INTENTIONAL (new feature), update EXPECTED_ROUTES in
the same commit that changes the surface — never to "make the test pass"
during a refactor.
"""

from __future__ import annotations

EXPECTED_ROUTES = [
    "DELETE /api/admin/allowlist/domains/{domain}",
    "DELETE /api/admin/allowlist/users/{email}",
    "DELETE /api/admin/auth-providers/{provider_id}",
    "DELETE /api/admin/blocklist/{email}",
    "DELETE /api/admin/groups/{group_id}",
    "DELETE /api/admin/groups/{group_id}/members/{user_id}",
    "DELETE /api/admin/impersonate",
    "DELETE /api/admin/users/{user_id}",
    "DELETE /api/auth/company-keys/{key_id}",
    "DELETE /api/auth/keys/{key_id}",
    "DELETE /api/folders/{folder_id}",
    "DELETE /api/folders/{folder_id}/dirs",
    "DELETE /api/folders/{folder_id}/files/{file_id}",
    "DELETE /api/folders/{folder_id}/sync",
    "DELETE /api/folders/{folder_id}/sync/error",
    "DELETE /api/jobs/cleanup-failed",
    "DELETE /api/sync/credentials/{cred_id}",
    "DELETE /mcp",
    "GET /",
    "GET /api/admin/allowlist",
    "GET /api/admin/auth-providers",
    "GET /api/admin/clerk/directory",
    "GET /api/admin/groups",
    "GET /api/admin/indexing-caps",
    "GET /api/admin/settings",
    "GET /api/admin/users",
    "GET /api/assets/{token}",
    "GET /api/auth/company-keys",
    "GET /api/auth/config",
    "GET /api/auth/google/callback",
    "GET /api/auth/keys",
    "GET /api/auth/login/google",
    "GET /api/auth/me",
    "GET /api/docs",
    "GET /api/files/{file_id}",
    "GET /api/files/{file_id}/email",
    "GET /api/files/{file_id}/images",
    "GET /api/files/{file_id}/layout",
    "GET /api/files/{file_id}/page-images",
    "GET /api/files/{file_id}/raw",
    "GET /api/files/{file_id}/stl",
    "GET /api/files/{file_id}/text",
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
    "GET /api/health",
    "GET /api/images/{image_id}",
    "GET /api/jobs/recent",
    "GET /api/openapi.json",
    "GET /api/sync/credentials",
    "GET /api/sync/local/accounts",
    "GET /api/sync/local/browse",
    "GET /api/sync/nfs/browse",
    "GET /api/sync/nfs/status",
    "GET /api/sync/oauth/google/callback",
    "GET /api/sync/oauth/microsoft/callback",
    "GET /api/users",
    "GET /api/users/me",
    "GET /favicon.ico",
    "GET /healthz",
    "PATCH /api/admin/auth-providers/{provider_id}",
    "PATCH /api/admin/groups/{group_id}",
    "PATCH /api/admin/indexing-caps",
    "PATCH /api/admin/settings",
    "PATCH /api/admin/users/{user_id}",
    "PATCH /api/folders/{folder_id}/active",
    "PATCH /api/folders/{folder_id}/rename",
    "PATCH /api/folders/{folder_id}/share",
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
    "POST /api/auth/account/{account_id}",
    "POST /api/auth/company-keys",
    "POST /api/auth/keys",
    "POST /api/auth/logout",
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
    "POST /api/jobs/cancel-all",
    "POST /api/jobs/retry-failed",
    "POST /api/jobs/{job_id}/cancel",
    "POST /api/jobs/{job_id}/retry",
    "POST /api/search",
    "POST /api/sync/credentials",
    "POST /api/sync/credentials/import-from-folder/{folder_id}",
    "POST /api/sync/credentials/{cred_id}/google/auth",
    "POST /api/sync/local/connect",
    "POST /api/users",
    "POST /mcp",
    "PUT /api/folders/{folder_id}/sync",
]


def test_route_surface_unchanged(env: None) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    actual = sorted(
        {
            f"{m} {r.path}"
            for r in app.routes
            if hasattr(r, "methods")
            for m in r.methods
            if m != "HEAD"
        }
    )
    missing = sorted(set(EXPECTED_ROUTES) - set(actual))
    added = sorted(set(actual) - set(EXPECTED_ROUTES))
    assert not missing and not added, (
        f"Route surface changed.\nMissing: {missing}\nAdded: {added}"
    )
