# voitta-image-rag — Architecture

A clean-slate rewrite of voitta-rag with first-class image support, content-addressable extraction, websocket-driven UI, and a pluggable sync layer (future).

---

## 1. Goals

1. **Filesystem-driven indexing.** A folder is registered; the watcher detects file add/change/delete; the pipeline reacts. No manual "sync" or "embed" buttons.
2. **Content-addressable extraction.** Parsed text and extracted images live on disk under a CAS keyed by source-file SHA-256. The SQLite DB stores metadata only.
3. **Multi-modal index.** Text chunks indexed with dense (e5-base-v2) + sparse (Qdrant BM25). Images indexed with a CLIP-family dual encoder (SigLIP-2 / OpenCLIP) so they are searchable by text *or* image.
4. **Image ↔ chunk linkage.** Every image carries a list of nearby chunks (with chunk-index distance); every chunk carries a list of nearby images. Both directions queryable.
5. **Real-time UI.** Thin SPA shell, all data over WebSocket. Progress, errors, and state changes stream live.
6. **Multi-user with single-user fallback.** ACLs by default; a config flag collapses everything to a single `root` user.
7. **Pluggable sync (provisioned).** Sync connectors *write into the watched filesystem*; they don't bypass it. So the pipeline downstream of the watcher is uniform.

Non-goals (v1): authoring/editing files in-app; document-level versioning beyond "latest"; cross-tenant isolation beyond ACLs.

---

## 2. Component Map

```
┌────────────────────────────────────────────────────────────────────────┐
│                         FastAPI process                                 │
│                                                                         │
│   ┌──────────┐    ┌──────────────┐    ┌────────────────────────────┐    │
│   │  Watcher │───▶│  Job Queue   │───▶│  Worker Pool (asyncio)     │    │
│   │ watchdog │    │  (SQLite)    │    │  ┌──────┐ ┌──────┐ ┌─────┐ │    │
│   └──────────┘    └──────────────┘    │  │Parse │ │Embed │ │Embed│ │    │
│        ▲                              │  │      │ │ text │ │image│ │    │
│        │                              │  └──┬───┘ └──┬───┘ └──┬──┘ │    │
│   ┌────┴─────┐                        └─────┼────────┼────────┼────┘    │
│   │   FS     │                              ▼        ▼        ▼         │
│   │ folders  │                          ┌─────┐  ┌────────────────┐     │
│   │ (config) │                          │ CAS │  │   Qdrant       │     │
│   └──────────┘                          │ /fs │  │ chunks, images │     │
│                                         └─────┘  └────────────────┘     │
│                                              ▲                          │
│   ┌──────────┐    ┌──────────┐               │                          │
│   │  HTTP    │    │   WS     │───────────────┤                          │
│   │  /api    │    │  events  │      ┌────────┴─────┐                    │
│   └────┬─────┘    └────┬─────┘      │   SQLite DB  │                    │
│        ▼               ▼            │ (metadata)   │                    │
│   ┌────────────────────────┐        └──────────────┘                    │
│   │   SPA (Solid + Vite)   │                                            │
│   └────────────────────────┘                                            │
│                                                                         │
│   ┌────────────────────────────────────────────────────────────────┐    │
│   │   MCP server (separate port) — search, get_file, get_chunks,   │    │
│   │   resolve_url, search_images, get_chunk_images                 │    │
│   └────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

Single Python process. Two ASGI apps (web on `:8000`, MCP on `:8001`). Workers are asyncio tasks inside the web process; a config flag can later split them out.

---

## 3. Data Storage

### 3.1 Layout

`VOITTA_DATA_DIR` (default `~/.voitta-image-rag/`):

```
voitta.db                          SQLite — metadata, jobs, ACLs
qdrant/                            Embedded Qdrant data (or remote URL)
cas/
  files/<file_sha>/
    text.md                        Extracted markdown
    manifest.json                  Parser version, image positions, chunk anchors
  images/<image_sha>.bin           Image bytes, deduped across files
                                   (mime stored in SQLite images.mime, not the filename)
jobs/
  logs/<job_id>.log                Per-job logs (rotating)
```

The original source bytes are not copied into CAS in v1 — we re-read from the live file in the watched folder. This keeps CAS purely a derived-artefact store.

**Why two CAS namespaces.** Files and images are deduplicated on different identities (file SHA vs. image SHA). Storing image bytes under the file's CAS dir would defeat cross-file image dedup, so each gets its own top-level namespace.

### 3.2 CAS Identity

- **`file_cas_id`**: `sha256(file_bytes)`. Unique key for an extracted file. Two files with identical bytes (across folders) share `cas/files/<file_sha>/` and skip re-extraction.
- **`image_cas_id`**: `sha256(image_bytes)`. Two images with identical bytes share a single blob at `cas/images/<image_sha>.bin` *and* a single Qdrant point — the same logo across docs is embedded once.
- **Reference counting** in SQLite (`cas_refs`):
  - `kind='file'` counts how many `files` rows hold a given `file_cas_id`.
  - `kind='image'` counts how many `images` rows hold a given `image_cas_id`.
  - On decrement-to-zero, the GC sweeper removes the corresponding CAS path (after a quiet-period grace to avoid TOCTOU with in-flight extracts).

### 3.3 SQLite Schema

```sql
-- Users & ACLs (carry forward voitta-rag's model)
CREATE TABLE users (
    id           INTEGER PRIMARY KEY,
    email        TEXT UNIQUE NOT NULL,
    display_name TEXT,
    created_at   INTEGER NOT NULL
);

-- Watched folders (the "projects"). Each root is a normal-folder source.
CREATE TABLE folders (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,    -- absolute host path
    display_name  TEXT NOT NULL,
    source_type   TEXT NOT NULL DEFAULT 'filesystem',  -- 'filesystem' | future: 'gdrive', 'github', ...
    source_config TEXT,                    -- JSON, source-specific
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL
);

-- One row per file under a watched folder.
CREATE TABLE files (
    id              INTEGER PRIMARY KEY,
    folder_id       INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    rel_path        TEXT NOT NULL,         -- relative to folder.path
    file_cas_id     TEXT,                  -- sha256 of bytes; NULL until first hash
    size_bytes      INTEGER,
    mtime_ns        INTEGER,               -- last fs mtime observed
    added_at        INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,      -- updated by watcher/scan
    last_indexed_at INTEGER,               -- success time
    state           TEXT NOT NULL,         -- see "File state machine" below
    pending_embeds  INTEGER NOT NULL DEFAULT 0,  -- count of outstanding embed_* jobs
    source_url      TEXT,                  -- external URL (set by sync connectors via .voitta_sources.json)
    error           TEXT,
    UNIQUE (folder_id, rel_path)
);
CREATE INDEX idx_files_state ON files(state);
CREATE INDEX idx_files_cas ON files(file_cas_id);

-- Chunks produced from a file (text only). Stored in SQLite for cheap range
-- queries; vector lives in Qdrant under the same chunk_id.
CREATE TABLE chunks (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,         -- 0-based ordering within file
    chunk_hash   TEXT NOT NULL,            -- sha256 of normalized text — for diff
    text         TEXT NOT NULL,            -- the chunk's text
    char_start   INTEGER,                  -- offset into text.md
    char_end     INTEGER,
    created_at   INTEGER NOT NULL,
    UNIQUE (file_id, chunk_index)
);
CREATE INDEX idx_chunks_hash ON chunks(chunk_hash);

-- Images extracted from a file (or a file that *is* an image).
CREATE TABLE images (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    image_index   INTEGER NOT NULL,        -- 0-based ordering within file
    image_cas_id  TEXT NOT NULL,           -- sha256 of image bytes
    anchor_chunk  INTEGER,                 -- chunk_index where the image sits (NULL for standalone-image files)
    page          INTEGER,                 -- if applicable (PDF/PPTX)
    width         INTEGER,
    height        INTEGER,
    mime          TEXT,
    created_at    INTEGER NOT NULL,
    UNIQUE (file_id, image_index)
);
CREATE INDEX idx_images_cas ON images(image_cas_id);
CREATE INDEX idx_images_anchor ON images(file_id, anchor_chunk);

-- Many-to-many: which chunks "see" which images, with chunk-index distance.
CREATE TABLE chunk_image_links (
    chunk_id  INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    image_id  INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    distance  INTEGER NOT NULL,            -- |chunk_index - image.anchor_chunk|
    PRIMARY KEY (chunk_id, image_id)
);
CREATE INDEX idx_cil_image ON chunk_image_links(image_id);

-- ACLs: which users may see which files. Folder-level grants expand at insert.
CREATE TABLE file_acl (
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, user_id)
);

CREATE TABLE folder_acl (
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (folder_id, user_id)
);

-- CAS reference counting. A single SHA can validly appear in both 'file' and
-- 'image' rows (e.g. an image file whose bytes are the file payload), so the
-- primary key is composite.
CREATE TABLE cas_refs (
    cas_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,         -- 'file' | 'image'
    refcount        INTEGER NOT NULL DEFAULT 0,
    last_decref_at  INTEGER,
    PRIMARY KEY (cas_id, kind)
);

-- Job queue.
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,           -- 'extract'|'embed_text'|'embed_image'|'delete_file'|'reindex_folder'|'gc_cas'
    payload       TEXT NOT NULL,           -- JSON
    state         TEXT NOT NULL,           -- 'queued'|'running'|'done'|'error'|'cancelled'
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    dedup_key     TEXT,                    -- e.g. "extract:<file_id>"; coalesces enqueues while in flight
    error         TEXT,
    enqueued_at   INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER
);
CREATE INDEX idx_jobs_state ON jobs(state, priority DESC, id);
-- One in-flight job per dedup_key; duplicate enqueues become no-ops.
CREATE UNIQUE INDEX idx_jobs_dedup_inflight
    ON jobs(dedup_key)
    WHERE dedup_key IS NOT NULL AND state IN ('queued', 'running');
```

#### File state machine

```
                  enqueue extract
   pending  ─────────────────────────▶  extracting
                                              │  parser+chunking ok
                                              ▼
   error  ◀── parser fails ──┐         extracted
                             │              │  enqueue embed_text + N×embed_image
                             │              │  pending_embeds = 1 + N
                             │              ▼
                             └─── any ──  embedding
                                              │  every embed_* completion: pending_embeds--
                                              │  when pending_embeds == 0:
                                              ▼
                                          indexed
                                              │
                                              ▼  watcher: file removed
                                          deleted
```

- A standalone image file has `pending_embeds = 1` (just `embed_image`); `embed_text` is skipped because there are no chunks.
- A file with text but no images skips the per-image fan-out; `pending_embeds = 1`.
- `pending_embeds` is incremented atomically with job enqueue, decremented in the same transaction that marks the embed job `done`.
- A re-extract resets the counter — `extract` deletes old chunks/images, then re-fans-out.

### 3.4 Qdrant Collections

**`chunks`** — one point per chunk, point ID = SQLite `chunks.id` (uint64):
- vectors (named):
  - `dense`: 768d (e5-base-v2)
  - `sparse`: BM25 sparse vector (Qdrant fastembed)
- payload: `chunk_id`, `file_id`, `folder_id`, `file_path`, `chunk_index`, `allowed_users: int[]`, `source_url`, `nearby_image_ids: int[]`, `dense_model_version`, `sparse_model_version`

**`images`** — one point per *unique image SHA*, point ID = SQLite `images.id` of the first row that introduced the SHA:
- vectors (named):
  - `image`: CLIP-image vector (dim depends on model; SigLIP-2 base = 768)
- payload: `image_id`, `image_cas_id`, `file_ids: int[]` (every file that contains this image), `folder_ids: int[]`, `allowed_users: int[]`, `image_model_version`

Cross-modal text→image search uses the **CLIP text encoder** at query time, on the same vector space as `image`. No second stored vector needed.

**Why `*_model_version` payload fields.** When a model is upgraded, points emitted by the old model still answer queries (correctly, since query and stored vectors must match). A migration job filters by `*_model_version != settings.<model>_version` and re-embeds in batches without a global rebuild.

**Image point ID note.** Multiple `images` rows can share an `image_cas_id` (same logo across files). The Qdrant point stores `file_ids: int[]` listing every owning file; on delete, we remove a file's id from that list, and only delete the point when the list goes empty (mirroring `cas_refs.kind='image'`).

### 3.5 Source URLs and `resolve_url`

External URLs (Google Doc links, GitHub blob URLs, etc.) are populated by **sync connectors** writing a `.voitta_sources.json` sidecar at the watched-folder root:

```json
{
  "designs/spec.gdoc": "https://docs.google.com/document/d/.../edit",
  "src/main.py":       "https://github.com/org/repo/blob/main/src/main.py"
}
```

The extractor reads this sidecar (if present) and copies the URL into `files.source_url`. From there it propagates into every chunk's Qdrant payload. For folders with `source_type = 'filesystem'` (no connector) the sidecar is absent and `source_url` is `NULL` — that's fine.

The `resolve_url` MCP tool / API endpoint reverses the lookup: given an external URL, return matching file(s) and (optionally) the chunk range. Implementation: SQL `SELECT * FROM files WHERE source_url = ?`. Sub-URL matches (e.g. `#anchor` fragments) fall back to prefix matching.

---

## 4. Pipeline

### 4.1 Watcher

`watchdog` observers, one per registered folder. Events coalesced into "file changed" intents (debounce ~500ms per path). For each intent:

- `created` / `modified` → enqueue `extract` job
- `deleted` → enqueue `delete_file` job (chunks, images, qdrant points, CAS refcount decrement)
- `moved` → treat as delete + create unless rename within folder (then update `rel_path`)

On startup, a **reconciliation scan** walks every enabled folder and:
- inserts missing `files` rows (state=`pending`)
- detects vanished files (state=`deleted`)
- compares mtime against `last_indexed_at`; if newer, enqueue `extract`

### 4.2 Job Queue

SQLite-backed with `SELECT ... ORDER BY priority DESC, id LIMIT 1 ... UPDATE state='running'` (single-writer; safe with WAL + `BEGIN IMMEDIATE`). N async workers (`VOITTA_WORKERS`, default = CPU count) poll. Long-running CPU-bound work (PDF parsing, embedding) runs in a `ProcessPoolExecutor` whose workers warm-load models once at boot; I/O-bound (Qdrant calls) runs directly in asyncio.

**Idempotency.** Every enqueue carries a `dedup_key` (e.g. `"extract:42"`, `"embed_text:42"`, `"embed_image:99"`). The partial unique index `idx_jobs_dedup_inflight` rejects a second insert while a job with the same key is `queued` or `running`; `enqueue()` catches the unique violation and returns the existing job's id. This collapses watcher event storms (rapid save→save→save) into a single in-flight extract.

Job kinds:

| Kind            | Trigger              | Effect |
|-----------------|----------------------|--------|
| `extract`       | watcher              | parse file → CAS write → upsert chunks/images rows → enqueue `embed_text` and (per image) `embed_image` |
| `embed_text`    | extract              | embed all chunks of a file in one batch → upsert qdrant `chunks` |
| `embed_image`   | extract              | embed image → upsert qdrant `images` (dedup by `image_cas_id`) |
| `delete_file`   | watcher              | drop chunks/images, qdrant points, decrement CAS refs |
| `reindex_folder`| user action          | enqueue `extract` for every file in folder |
| `gc_cas`        | startup, hourly      | sweep cas_refs where refcount=0 |

### 4.3 Reindex Strategy

Per your call: **whole-file reindex on any change.** No chunk-level diffing in v1. Sequence:

1. `extract` reads new bytes; computes `new_file_cas_id`.
2. If `new_file_cas_id == files.file_cas_id` → no-op (mtime updated, nothing else).
3. Else: delete all existing chunks/images/qdrant-points for the file; decrement CAS refs; write fresh CAS dir; insert fresh chunks/images.

Image deduplication still helps: if a logo recurs across versions, its CAS dir survives, and the next `embed_image` short-circuits.

### 4.4 Parser Contract

Extends voitta-rag's `BaseParser` to emit images and positions:

```python
@dataclass
class ExtractedImage:
    bytes: bytes
    mime: str
    position: int          # char offset into the produced markdown
    page: int | None        # for PDF/PPTX
    width: int | None
    height: int | None

@dataclass
class ParserResult:
    content: str            # Markdown
    images: list[ExtractedImage]
    metadata: dict
    success: bool
    error: str | None
```

Parsers are reused from voitta-rag. They are extended one-by-one (PDF, DOCX, PPTX yield images natively via PyMuPDF / python-docx / python-pptx; text-only parsers return `images=[]`). A standalone image file (`.jpg`/`.png`/`.webp`) goes through a tiny `ImageFileParser` that returns empty `content` and one `ExtractedImage`.

### 4.5 Image ↔ Chunk Linkage

After chunking the markdown:

1. For each image, find the chunk whose `[char_start, char_end)` contains `image.position`. **Fallback** if `position` falls in an inter-chunk gap (chunkers don't always cover every byte): pick the chunk minimising `min(|position - char_start|, |position - char_end|)`. That chunk becomes `anchor_chunk`.
2. For each image, link to chunks with `|chunk_index - anchor_chunk| ≤ N` (default `N=2`). Insert `chunk_image_links` rows with the distance.
3. Standalone-image files get no synthetic chunk — the image is anchored to no chunk (`anchor_chunk = NULL`). It still indexes and is searchable cross-modally; it just has no `nearby_chunks` relation.

At query time:
- "Get a chunk's images": `SELECT image_id, distance FROM chunk_image_links WHERE chunk_id = ? ORDER BY distance`.
- "Get an image's chunks": symmetric.
- Qdrant payload caches `nearby_image_ids` / `nearby_chunk_ids` so search results carry the relation without a second SQLite hit.

---

## 5. Search

Three search surfaces, all through one router:

### 5.1 `text → chunks` (primary RAG)
- e5-base-v2 dense + Qdrant BM25 sparse, fused with **RRF** (Reciprocal Rank Fusion).
- Filters: folder, ACL (`allowed_users` contains user), folder enabled.

### 5.2 `text → images` (cross-modal)
- CLIP text encoder on query → vector search against `images.image` vector.
- Filters: folder, ACL.
- Returns image points + their `nearby_chunk_ids` so callers can hydrate context.

### 5.3 `image → images` (reverse image search)
- CLIP image encoder on uploaded query image → same vector search.

A unified `/api/search` endpoint takes `{query: str | image, modes: ['chunks', 'images']}` and returns both modalities, ranked separately.

---

## 6. WebSocket Protocol

Single `/ws` endpoint, one connection per browser session.

**Server → client events** (JSON, `{type, ...}`):

| Type                | Payload                                                          |
|---------------------|------------------------------------------------------------------|
| `folder.added`      | `{folder}`                                                       |
| `folder.removed`    | `{folder_id}`                                                    |
| `file.upserted`     | `{file: {id, folder_id, rel_path, state, last_indexed_at}}`      |
| `file.deleted`      | `{file_id}`                                                      |
| `job.started`       | `{job_id, kind, payload}`                                        |
| `job.progress`      | `{job_id, current, total, message}`                              |
| `job.finished`      | `{job_id, state: 'done'|'error', error?}`                        |
| `stats`             | `{queued, running, indexed, errored}` — periodic                 |
| `search.partial`    | `{query_id, results}` — for streaming search results             |

**Client → server events**:

| Type                | Payload                                                          |
|---------------------|------------------------------------------------------------------|
| `subscribe`         | `{topics: ['files', 'jobs', 'stats']}`                           |
| `search`            | `{query_id, query, modes, filters}`                              |
| `cancel`            | `{query_id}`                                                     |

The server keeps a per-connection topic subscription; events route through an internal pub/sub (`asyncio.Queue` per connection, single broker task).

REST endpoints exist for create/delete actions (folders, users) — they're just easier to authorise. Read-side state flows entirely through WS after an initial snapshot.

---

## 7. Frontend

**Stack:** Vite + Solid.js + TypeScript. Tailwind for styles (small, no theme system needed in v1).

**Layout:**

- Left: folder list + file tree (live-updated from WS).
- Center: file viewer (markdown render of `text.md`, with image thumbnails inline at their `position`; clickable to open original).
- Right: panel — search box, results, job/stat dashboard.

**Inline image rendering.** Parsers don't always emit markdown image syntax (`![]()`) for extracted images — they record a `(position, image_id)` for each. The viewer fetches the file's `images[]` metadata, sorts by `position` descending, and **splices `<img src="/api/images/{image_id}/thumb">` tags into the markdown source at each `position`** before passing to a markdown renderer (`marked` + `DOMPurify`). Descending order avoids invalidating earlier offsets. PDF/PPTX images include a `page` overlay caption.

**State model:** one Solid store per resource (folders, files, jobs, search). The WS handler updates stores; components subscribe. Optimistic updates on user-initiated actions (folder create, file delete) reconcile against WS confirmations.

**Bundle:** Vite builds to `static/dist/`. FastAPI serves a single `index.html` shell from a Jinja template (so we can inject CSRF / user info), everything else is `<script type="module" src="/static/dist/main.js"></script>`.

---

## 8. MCP Server

Mounted at `/mcp` on a separate port (carry voitta-rag's pattern). Tools:

| Tool                       | Returns                                                     |
|----------------------------|-------------------------------------------------------------|
| `search`                   | text→chunks RRF results                                     |
| `search_images`            | text→images CLIP results                                    |
| `get_file`                 | full text.md for a file (or pre-signed URL)                 |
| `get_chunk_range`          | chunks `[i..j]` of a file                                   |
| `get_chunk_images`         | image refs (id, distance, page) for a chunk                 |
| `get_image`                | image bytes / URL by `image_id`                             |
| `list_indexed_folders`     | folders + index status                                      |
| `resolve_url`              | external URL → file/chunks (from `source_url` payload)      |

ACLs enforced by passing the caller's user (via `X-User-Name` header, as today).

---

## 9. Authentication

Three modes, picked at install time (do **not** toggle at runtime — `allowed_users` payloads bake the mode in):

- **Multi-user mode** (default): a `users.txt` seed file lists allowed emails (matching voitta-rag's pattern). Browser auth comes via a header set by a reverse proxy (e.g. `X-Forwarded-Email`); MCP uses `X-User-Name`. The reverse proxy is out of scope to implement here.
- **Dev mode** (`VOITTA_DEV_USER=<email>`): every request is authenticated as that user. Use for local dev when no proxy is fronting the app. Mutually exclusive with single-user mode.
- **Single-user mode** (`VOITTA_SINGLE_USER=true`): all requests collapse to user `root`; ACL filters become no-ops at query time; the indexing pipeline still writes `allowed_users=[root_user_id]` so the same data round-trips correctly if the install is later upgraded to multi-user (with `root`'s grants intact).

ACL evaluation happens at the SQL layer (joins to `file_acl`) and at the Qdrant layer (`allowed_users` payload filter). They must agree; the indexing pipeline writes both atomically (Qdrant first, then SQLite — see §13).

**Mode invariant.** Switching modes after data has been indexed is supported one-way (single → multi) because single-user always writes a real user id. Going multi → single is a config-only change, since the filter is a no-op anyway.

---

## 10. Sync Plugin Provisioning (no implementation in v1)

Future sync connectors (Google Drive, GitHub, Confluence, …) implement:

```python
class SyncConnector(Protocol):
    source_type: str         # 'gdrive', etc.
    config_schema: type      # pydantic model

    async def sync(self, folder: Folder, dest_dir: Path) -> SyncResult:
        """Pull remote → write files into dest_dir (a real directory under
        the watched folder root). The watcher picks them up. Connector also
        writes .voitta_sources.json sidecars for source_url tracking."""
```

Key invariants:
- **Connectors write to the filesystem; never to Qdrant or SQLite directly.** This keeps the indexing pipeline uniform.
- A folder's `source_type` = `filesystem` means "human edits"; anything else means "managed by connector X" and the UI hides write actions.
- A `sources/` directory inside `services/` will hold connector implementations behind a `registry` (mirror of `parsers/`).

v1 provides the abstract base class and the registry, no concrete connectors.

---

## 11. Configuration

All env-driven; `.env` file supported via `python-dotenv`.

| Var                       | Default                       | Meaning                                  |
|---------------------------|-------------------------------|------------------------------------------|
| `VOITTA_DATA_DIR`         | `~/.voitta-image-rag`         | parent for db/cas/qdrant                 |
| `VOITTA_DB_PATH`          | `$DATA/voitta.db`             | override db                              |
| `VOITTA_CAS_DIR`          | `$DATA/cas`                   | override cas                             |
| `VOITTA_QDRANT_URL`       | (embedded)                    | remote Qdrant URL; embedded if unset     |
| `VOITTA_QDRANT_PATH`      | `$DATA/qdrant`                | embedded data path                       |
| `VOITTA_PORT`             | `8000`                        | web port                                 |
| `VOITTA_MCP_PORT`         | `8001`                        | mcp port                                 |
| `VOITTA_WORKERS`          | cpu_count                     | async worker count                       |
| `VOITTA_DENSE_MODEL`      | `intfloat/e5-base-v2`         | text dense                               |
| `VOITTA_SPARSE_MODEL`     | `Qdrant/bm25`                 | text sparse (fastembed)                  |
| `VOITTA_IMAGE_MODEL`      | `google/siglip2-base-patch16-224` | image+text dual                      |
| `VOITTA_NEARBY_RADIUS`    | `2`                           | chunk distance for image linkage         |
| `VOITTA_SINGLE_USER`      | `false`                       | collapse to root user (see §9)           |
| `VOITTA_DEV_USER`         | (unset)                       | authenticate every request as this email |
| `VOITTA_USERS_FILE`       | `users.txt`                   | seed file (multi-user)                   |
| `VOITTA_IGNORE_PATTERNS`  | `.git,node_modules,.DS_Store,__pycache__,.venv,*.tmp` | comma-separated globs the watcher skips |
| `VOITTA_MAX_FILE_BYTES`   | `1073741824` (1 GiB)          | files larger than this are skipped       |
| `VOITTA_DENSE_VERSION`    | `e5-base-v2@1`                | written to chunk payloads; used for re-embed migrations |
| `VOITTA_SPARSE_VERSION`   | `bm25@1`                      | same, for sparse                         |
| `VOITTA_IMAGE_VERSION`    | `siglip2-base@1`              | same, for image                          |

---

## 12. Source Tree

```
voitta-image-rag/
  pyproject.toml
  README.md
  ARCHITECTURE.md
  IMPLEMENTATION_PLAN.md
  .env.example
  Makefile
  src/voitta_image_rag/
    __init__.py
    main.py                 # FastAPI app factory
    config.py
    db/
      __init__.py
      models.py             # SQLAlchemy models
      database.py           # engine, init, migrations
      schema.sql            # source-of-truth DDL (SQLAlchemy mirrors it)
    cas/
      __init__.py
      store.py              # write/read/refcount
      gc.py
    services/
      watcher.py
      job_queue.py
      worker.py             # the asyncio worker loop
      indexing.py           # extract → chunk → embed orchestration
      chunking.py
      embedding/
        __init__.py
        text.py             # e5
        sparse.py           # BM25
        image.py            # CLIP
      vector_store.py       # Qdrant adapter
      parsers/              # carry over from voitta-rag, extended for images
        __init__.py
        base.py
        registry.py
        pdf_parser.py
        docx_parser.py
        pptx_parser.py
        text_parser.py
        image_parser.py     # NEW — for standalone image files
        ...
      sources/              # sync plugin scaffold (no concretes in v1)
        __init__.py
        base.py
        registry.py
      events.py             # internal pub/sub broker
      acl.py
    api/
      __init__.py
      deps.py               # auth, db session
      routes/
        __init__.py
        folders.py
        files.py
        search.py
        users.py
      ws.py
    mcp_server.py
  static/
    index.html              # Jinja shell
    dist/                   # built SPA bundle (gitignored)
  ui/                       # Vite + Solid sources
    package.json
    vite.config.ts
    tsconfig.json
    index.html              # dev entry; build outputs to ../static/dist
    src/
      main.tsx
      ws.ts
      stores/
        folders.ts
        files.ts
        jobs.ts
        search.ts
      components/
        FolderList.tsx
        FileTree.tsx
        FileViewer.tsx
        SearchPanel.tsx
        JobMonitor.tsx
  scripts/
    seed_users.py
    rebuild_index.py
  tests/
    unit/
    integration/
```

---

## 13. Key Invariants

1. **A file's authoritative state is its bytes on disk.** SQLite caches metadata; CAS caches derived artefacts; Qdrant caches embeddings. All three are reproducible from a folder scan.
2. **CAS deduplication is best-effort, never load-bearing.** A bug that causes a CAS miss costs CPU, never correctness.
3. **Connectors don't bypass the watcher.** Anything that wants to add content writes a file.
4. **ACL writes are atomic.** A new chunk's SQLite row + Qdrant point land in the same transactional unit (write Qdrant first, then SQLite; on retry the Qdrant upsert is idempotent by `chunk_id`).
5. **WS is the source of truth for live state.** REST returns 200 immediately on mutations; clients wait for a WS confirmation to flip UI state.
