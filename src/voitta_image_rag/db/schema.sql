-- voitta-image-rag SQLite schema. See ARCHITECTURE.md §3.3.
-- Source of truth for DDL. Models in db/models.py mirror it.
-- v1 has no migrations: change this file and rebuild the DB.

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    email        TEXT UNIQUE NOT NULL,
    display_name TEXT,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    source_type   TEXT NOT NULL DEFAULT 'filesystem',
    source_config TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    managed       INTEGER NOT NULL DEFAULT 0,  -- 1 if created under VOITTA_ROOT_PATH; sync connectors require managed=1
    -- The user who registered the folder. They alone can rename, delete,
    -- toggle ``shared``, configure sync, reindex, upload, grant, revoke.
    -- NULL on legacy rows registered before ownership existed; the migration
    -- backfills from any folder_acl row.
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
    created_at    INTEGER NOT NULL,
    UNIQUE (file_id, image_index)
);
CREATE INDEX IF NOT EXISTS idx_images_cas ON images(image_cas_id);
CREATE INDEX IF NOT EXISTS idx_images_anchor ON images(file_id, anchor_chunk);

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
    source_type        TEXT NOT NULL,                        -- 'github' | 'google_drive'
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
    -- Status / bookkeeping
    sync_status        TEXT NOT NULL DEFAULT 'idle',         -- 'idle' | 'syncing' | 'error'
    sync_error         TEXT,
    last_synced_at     INTEGER,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);

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
    finished_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, priority DESC, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup_inflight
    ON jobs(dedup_key)
    WHERE dedup_key IS NOT NULL AND state IN ('queued', 'running');
