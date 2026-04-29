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
    source_url      TEXT,
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

CREATE TABLE IF NOT EXISTS cas_refs (
    cas_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- 'file' | 'image'
    refcount        INTEGER NOT NULL DEFAULT 0,
    last_decref_at  INTEGER,                 -- Unix epoch seconds; set when refcount drops to 0
    PRIMARY KEY (cas_id, kind)               -- a SHA can validly be both a file and an image
);

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
