-- voitta-image-rag SQLite schema. See ARCHITECTURE.md §3.3.
-- Source of truth for DDL. Models in db/models.py mirror it.
-- v1 has no migrations: change this file and rebuild the DB.

-- One row per ACCOUNT, not per person: a user signing in via the Clerk
-- directory gets one row per Clerk organization (company_id = Clerk org id,
-- e.g. 'org_3FP…') plus the reserved Personal account (company_id = '').
-- Native-only users have just the Personal row. Everything downstream
-- (folders.owner_id, folder_acl, api_keys, groups) hangs off users.id and
-- is therefore account-scoped for free. company_name is display-only and
-- refreshed from Clerk at login; identity is (email, company_id).
-- Admin status is PERSON-level by convention: checks look at every row
-- sharing the email (see api/deps.py); the flag is stamped on all of them.
-- Existing DBs are rebuilt into this shape by _migrate_users_accounts()
-- in db/database.py (the old email-UNIQUE constraint must go away).
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    email        TEXT NOT NULL,
    company_id   TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    display_name TEXT,
    -- 1 = full admin: can edit allowlist/blocklist, toggle other admins,
    -- and impersonate other users. Bootstrap admins listed in
    -- VOITTA_SUPER_ADMINS get is_admin=1 stamped on every sign-in.
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    UNIQUE (email, company_id)
);

CREATE TABLE IF NOT EXISTS folders (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    source_type   TEXT NOT NULL DEFAULT 'filesystem',
    source_config TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- The user who registered the folder. They alone can rename, delete,
    -- toggle ``shared``, configure sync, reindex, upload, grant, revoke.
    owner_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- When 1, every signed-in user sees this folder (read-only) regardless
    -- of folder_acl. Owner-toggleable.
    shared        INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY,
    folder_id       INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    rel_path        TEXT NOT NULL,
    file_cas_id     TEXT,
    size_bytes      INTEGER,
    mtime_ns        INTEGER,
    added_at        INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    last_indexed_at INTEGER,
    state           TEXT NOT NULL,
    pending_embeds  INTEGER NOT NULL DEFAULT 0,
    embed_round     INTEGER NOT NULL DEFAULT 0,
    source_url      TEXT,
    tab             TEXT,                                 -- e.g. Google Docs tab name when the file is one tab of a multi-tab doc
    source_meta     TEXT,                                 -- JSON: source provenance (owner/creator/editor/shared_by + created/modified epochs) → meta_* Qdrant payload
    error           TEXT,
    UNIQUE (folder_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_state ON files(state);
CREATE INDEX IF NOT EXISTS idx_files_cas ON files(file_cas_id);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    chunk_hash   TEXT NOT NULL,
    text         TEXT NOT NULL,
    char_start   INTEGER,
    char_end     INTEGER,
    created_at   INTEGER NOT NULL,
    UNIQUE (file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(chunk_hash);

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    image_index   INTEGER NOT NULL,
    image_cas_id  TEXT NOT NULL,
    anchor_chunk  INTEGER,
    page          INTEGER,
    width         INTEGER,
    height        INTEGER,
    mime          TEXT,
    -- Discriminator. 'figure' = a crop extracted from a page (default,
    -- and what every parser produced before this column existed); these
    -- are the rows that get SigLIP-embedded for image search and linked
    -- into chunk_image_links. 'page_render' = a full-page raster captured
    -- as layout context for the LLM; not embedded, no anchor_chunk, no
    -- chunk links — fetched on demand via the MCP get_page_image tool.
    kind          TEXT NOT NULL DEFAULT 'figure',
    created_at    INTEGER NOT NULL,
    UNIQUE (file_id, image_index)
);
CREATE INDEX IF NOT EXISTS idx_images_cas ON images(image_cas_id);
CREATE INDEX IF NOT EXISTS idx_images_anchor ON images(file_id, anchor_chunk);
CREATE INDEX IF NOT EXISTS idx_images_kind ON images(file_id, kind, page);

CREATE TABLE IF NOT EXISTS chunk_image_links (
    chunk_id  INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    image_id  INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    distance  INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, image_id)
);
CREATE INDEX IF NOT EXISTS idx_cil_image ON chunk_image_links(image_id);

CREATE TABLE IF NOT EXISTS file_acl (
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, user_id)
);

CREATE TABLE IF NOT EXISTS folder_acl (
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (folder_id, user_id)
);

-- Organizational user groups. Purely a labelling/membership layer for now —
-- groups do NOT affect folder visibility (that wiring is a later feature).
CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_groups (
    user_id  INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);
CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups(group_id);

-- Per-user, per-folder MCP-search opt-out. ``active=0`` means the folder
-- is hidden from this user's MCP search calls (and the SPA renders the
-- toggle off, but the folder is still visible/expandable). Default-on is
-- represented by the absence of a row, so brand-new folders / brand-new
-- users automatically include everything.
CREATE TABLE IF NOT EXISTS folder_user_settings (
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    active    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (folder_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_folder_user_settings_user
    ON folder_user_settings(user_id);

CREATE TABLE IF NOT EXISTS cas_refs (
    cas_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- 'file' | 'image'
    refcount        INTEGER NOT NULL DEFAULT 0,
    last_decref_at  INTEGER,                 -- Unix epoch seconds; set when refcount drops to 0
    PRIMARY KEY (cas_id, kind)               -- a SHA can validly be both a file and an image
);

-- Per-folder remote sync configuration. Provider-specific columns are namespaced
-- by prefix (gh_*, gd_*, …). Add new prefixes as connectors land.
CREATE TABLE IF NOT EXISTS folder_sync_sources (
    folder_id          INTEGER PRIMARY KEY REFERENCES folders(id) ON DELETE CASCADE,
    source_type        TEXT NOT NULL,                        -- 'github' | 'google_drive' | 'nfs'
    -- GitHub
    gh_repo            TEXT,                                 -- HTTPS or git@ URL
    gh_path            TEXT,                                 -- subfolder within the repo
    gh_branches        TEXT,                                 -- JSON array, ignored when gh_all_branches=1
    gh_all_branches    INTEGER NOT NULL DEFAULT 0,
    gh_extended        INTEGER NOT NULL DEFAULT 0,           -- also mirror per-commit history
    gh_auth_method     TEXT,                                 -- 'ssh' | 'token'
    gh_username        TEXT,
    gh_pat             TEXT,                                 -- personal access token
    gh_token           TEXT,                                 -- SSH private key (PEM)
    -- Google Drive
    gd_client_id              TEXT,                          -- OAuth2 client ID
    gd_client_secret          TEXT,                          -- OAuth2 client secret
    gd_refresh_token          TEXT,                          -- set by the OAuth callback, not the save endpoint
    gd_service_account_json   TEXT,                          -- alternative auth (server-to-server)
    gd_folder_id              TEXT,                          -- root Drive folder or shared-drive ID
    gd_use_loopback           INTEGER NOT NULL DEFAULT 0,    -- 1 = OAuth redirect via http://localhost:53682 (admin runs a local nginx bridge that proxies the callback back to this server)
    gd_use_builtin            INTEGER NOT NULL DEFAULT 0,    -- 1 = OAuth creds from VOITTA_GD_BUILTIN_CLIENT_ID/SECRET (desktop built-in client), not this row
    gd_files_only             INTEGER NOT NULL DEFAULT 0,    -- 1 = sync binary files only, skip native Docs/Sheets/Slides/Forms (only the Drive API is then required)
    gd_credential_id          INTEGER REFERENCES sync_credentials(id) ON DELETE SET NULL,  -- shared company credential; when set, inline gd_* auth fields are ignored
    -- NFS (admin-defined root path + user-chosen subpath underneath).
    -- The connector mirrors files from ``<admin nfs_root>/<nfs_subpath>``
    -- into the folder's filesystem storage, same lifecycle as Drive.
    nfs_subpath               TEXT,                          -- POSIX relative path under the admin-set NFS root
    -- Status / bookkeeping
    sync_status        TEXT NOT NULL DEFAULT 'idle',         -- 'idle' | 'syncing' | 'error'
    sync_error         TEXT,
    last_synced_at     INTEGER,
    -- Periodic auto-sync. When ``auto_sync_enabled=1``, the in-process
    -- scheduler enqueues a sync job whenever ``last_synced_at`` is older
    -- than ``auto_sync_hours`` (1-24). Manually triggered syncs still work
    -- alongside this — both feed the same dedup'd queue.
    auto_sync_enabled  INTEGER NOT NULL DEFAULT 0,
    auto_sync_hours    INTEGER NOT NULL DEFAULT 6,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);

-- Company-scoped reusable sync credentials ("configure the Google client
-- once per org"). A folder_sync_sources row can reference one via
-- gd_credential_id instead of carrying inline gd_* credentials. The OAuth
-- refresh_token lives HERE for oauth-client credentials, so one consent
-- serves every folder that references the credential. No per-credential
-- ACL: visibility is the company (company_id = Clerk org id, '' = native
-- deployment space) — deliberate, mirrors company_api_keys.
CREATE TABLE IF NOT EXISTS sync_credentials (
    id                    INTEGER PRIMARY KEY,
    company_id            TEXT NOT NULL DEFAULT '',
    kind                  TEXT NOT NULL,                -- 'google_oauth_client' | 'google_service_account'
    label                 TEXT NOT NULL DEFAULT '',
    client_id             TEXT NOT NULL DEFAULT '',     -- oauth-client kind
    client_secret         TEXT NOT NULL DEFAULT '',     -- oauth-client kind
    service_account_json  TEXT NOT NULL DEFAULT '',     -- service-account kind
    refresh_token         TEXT NOT NULL DEFAULT '',     -- set by the OAuth callback, not create
    connected_email       TEXT NOT NULL DEFAULT '',     -- who granted consent (display only, best-effort)
    created_by            TEXT NOT NULL DEFAULT '',     -- acting user email
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_credentials_company
    ON sync_credentials(company_id, kind);

-- Personal API keys minted from the Settings panel. The token's plaintext
-- is shown to the user exactly once at creation; only the SHA-256 hash is
-- stored, plus a short prefix for UI display ("vk_abc1…"). MCP auth will
-- look up by hash and bump last_used_at on every accepted call.
CREATE TABLE IF NOT EXISTS api_keys (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,                       -- user-facing label
    prefix         TEXT NOT NULL,                       -- first chars of the token, e.g. "vk_abc123"
    key_hash       TEXT NOT NULL UNIQUE,                -- sha256 hex of the full token
    created_at     INTEGER NOT NULL,
    last_used_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- Company API keys ("cvk_…"). One key grants access for a whole company
-- scope: a Clerk org (company_id='org_…') or the native deployment space
-- (company_id=''). A request must pair the key with a user email; the
-- email is verified against the scope (Clerk membership / native
-- allowlist) at request time and JIT-provisions the account row.
CREATE TABLE IF NOT EXISTS company_api_keys (
    id             INTEGER PRIMARY KEY,
    company_id     TEXT NOT NULL DEFAULT '',            -- Clerk org id, '' = native space
    company_name   TEXT NOT NULL DEFAULT '',            -- display-only snapshot
    name           TEXT NOT NULL,                       -- admin-facing label
    prefix         TEXT NOT NULL,                       -- first chars of the token, e.g. "cvk_abc12"
    key_hash       TEXT NOT NULL UNIQUE,                -- sha256 hex of the full token
    created_by     TEXT NOT NULL DEFAULT '',            -- email of the minting admin
    created_at     INTEGER NOT NULL,
    last_used_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_company_api_keys_company ON company_api_keys(company_id);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    payload       TEXT NOT NULL,
    state         TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    dedup_key     TEXT,
    error         TEXT,
    enqueued_at   INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER,
    -- JSON summary the handler returns on success (e.g. a sync's
    -- files_added/pages_written/errors). Surfaced in the Jobs panel's
    -- expandable detail; NULL for handlers that report nothing.
    result        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, priority DESC, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup_inflight
    ON jobs(dedup_key)
    WHERE dedup_key IS NOT NULL AND state IN ('queued', 'running');

-- Admin-managed list of OAuth provider credentials. No uniqueness besides
-- the primary key — two rows per provider are intentionally allowed.
-- Login flow currently consumes only Google rows where enabled=1; the
-- schema accepts microsoft/github values so the admin UI can list them
-- without the back end going to read them.
CREATE TABLE IF NOT EXISTS auth_providers (
    id             INTEGER PRIMARY KEY,
    provider       TEXT NOT NULL,                       -- "google" | "microsoft" | "github"
    label          TEXT NOT NULL DEFAULT '',
    client_id      TEXT NOT NULL,
    client_secret  TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1,
    source         TEXT NOT NULL DEFAULT 'user',        -- "user" | "env"
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_providers_provider
    ON auth_providers(provider, enabled);
