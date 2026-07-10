# Voitta RAG Enterprise — REST API quickstart

The REST API is the same surface the web UI uses — every data route the
SPA calls (folders, files, upload, sync, search, jobs, users, images)
accepts API-key auth. Interactive reference: **`/api/docs`** (Swagger UI,
sign-in or API key required); machine-readable schema:
**`/api/openapi.json`**.

## Authentication

Two key kinds, both minted in the UI (Settings → API keys):

```bash
# Personal key (vk_…) — the key IS the identity (the account it was minted under)
curl -H "Authorization: Bearer vk_XXXX" https://voitta.example.com/api/auth/me

# Company key (cvk_…) — pair it with the acting user's email…
curl -H "Authorization: Bearer cvk_XXXX" \
     -H "X-Voitta-User-Email: alice@example.com" \
     https://voitta.example.com/api/auth/me

# …or embed the email in the token
curl -H "Authorization: Bearer cvk_XXXX:alice@example.com" \
     https://voitta.example.com/api/auth/me
```

`GET /api/auth/me` is the "whoami": use it to verify a key and see which
account (email + company scope) it resolves to.

**Cookie-only surface:** every identity-requiring route under
`/api/admin/*` and `/api/auth/*` — account switching, key management, the
whole admin console — rejects API keys with 403 (even an admin's key);
manage keys and admin settings in the browser. The public auth endpoints
(`/api/auth/config`, the Google login/callback pair, `logout`) don't
consult keys at all. Everything else is fair game.

## Folders

```bash
BASE=https://voitta.example.com/api
AUTH='Authorization: Bearer vk_XXXX'

# List my folders
curl -H "$AUTH" $BASE/folders

# Create a managed folder (single path segment under the server's VOITTA_ROOT_PATH)
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"name": "reports", "display_name": "Quarterly reports"}' \
     $BASE/folders                          # → 201 {id, path, …}

# Folder stats / file listing / delete
curl -H "$AUTH" $BASE/folders/42/stats
curl -H "$AUTH" $BASE/folders/42/files
curl -X DELETE -H "$AUTH" $BASE/folders/42
```

## Upload

Multipart; multiple files per request are fine. Files land in the folder
and are indexed automatically (watcher picks them up).

```bash
curl -X POST -H "$AUTH" \
     -F 'file=@q3-summary.pdf' -F 'file=@q3-data.xlsx' \
     "$BASE/folders/42/upload"              # → 201 {files, count, size_bytes}

# Into a subdirectory:
curl -X POST -H "$AUTH" -F 'file=@notes.md' \
     "$BASE/folders/42/upload?rel_dir=meetings/2026"
```

## Search

```bash
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"query": "Q3 EMEA revenue", "folder_ids": [42], "limit": 10}' \
     $BASE/search                           # → {chunks: [...], images: [...]}
```

Results are ACL-scoped to the key's account — same visibility as the UI.

## Sync

```bash
# Read a folder's sync config (secrets masked)
curl -H "$AUTH" $BASE/folders/42/sync

# Attach/update a connector (example: GitHub via PAT). The body is an
# envelope: source_type + a same-named block with that connector's fields.
curl -X PUT -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"source_type": "github",
          "github": {"repo": "https://github.com/org/repo",
                     "branches": ["main"],
                     "auth_method": "token", "pat": "ghp_…"},
          "auto_sync_enabled": true, "auto_sync_hours": 6}' \
     $BASE/folders/42/sync

# Trigger a sync now / check job status
curl -X POST -H "$AUTH" $BASE/folders/42/sync/trigger
curl -H "$AUTH" $BASE/jobs/recent
```

(Connector-specific fields — Google Drive, SharePoint, Teams, Confluence,
Jira, NFS — are all in `/api/docs`; OAuth-based connectors need their
browser consent flow completed once from the UI.)

## Files

```bash
curl -H "$AUTH" $BASE/files/1337          # metadata
curl -H "$AUTH" $BASE/files/1337/text     # extracted markdown
curl -H "$AUTH" $BASE/files/1337/raw -o original.pdf
curl -X DELETE -H "$AUTH" $BASE/folders/42/files/1337
```

## Notes

- All errors are JSON `{detail: …}`. 401 = bad/missing credential. 403 =
  authenticated but not allowed (a cookie-only route, or an owner-only
  mutation on a folder you can see but don't own). **404** = the resource
  either doesn't exist *or* isn't visible to you — folders, files, and
  images you have no access to return 404, not 403, so their ids aren't
  probeable. Reading any file/image is gated by its folder's visibility
  (owned, granted, community-shared, or single-user).
- A `vk_` key acts as the exact account it was minted under (mint while
  the right company account is active). A `cvk_` key acts as the given
  email **within the key's company scope**; members who never signed in
  are provisioned on first use.
- MCP access (for LLM agents) uses the same keys against `/mcp` — see
  [OPERATIONS.md §7](OPERATIONS.md#7-identity--accounts-sign-in-gate-clerk-directory-switching).
