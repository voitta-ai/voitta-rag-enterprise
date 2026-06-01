# Voitta RAG Enterprise — Operations & Data Flow

> A detailed, diagram-first walkthrough of how the system actually works at
> runtime: file ingestion, the job queue, the websocket event stream, search,
> the data model, the locking model, and — in depth — how **admin settings
> propagate** and how **admin-defined OAuth / sync providers surface to users**.
>
> Diagrams are [Mermaid](https://mermaid.js.org/). GitHub, VS Code (with the
> Mermaid extension), and most markdown viewers render them inline.

## Contents

1. [System overview](#1-system-overview)
2. [File ingestion pipeline](#2-file-ingestion-pipeline)
3. [Job queue mechanics](#3-job-queue-mechanics)
4. [File & job state machines](#4-file--job-state-machines)
5. [Event system & websocket propagation](#5-event-system--websocket-propagation)
6. [Search query path](#6-search-query-path)
7. [Admin settings: storage & propagation](#7-admin-settings-storage--propagation)
8. [OAuth providers: admin defines → user consumes](#8-oauth-providers-admin-defines--user-consumes)
9. [Sync OAuth runtime flows](#9-sync-oauth-runtime-flows)
10. [Data model](#10-data-model)
11. [Locking model](#11-locking-model)

---

## 1. System overview

The system is a single FastAPI process (`main.py`) with a background worker
pool. Inputs (filesystem watcher, startup scanner, sync connectors) enqueue
jobs; a worker drains them through the extract → chunk → embed pipeline;
results land in three stores (CAS blobs on disk, SQLite metadata, Qdrant
vectors). A websocket pushes live state to the vanilla-ESM SPA. An MCP server
exposes the same data to LLM agents.

```mermaid
flowchart TB
    subgraph clients["Clients"]
        SPA["Browser SPA<br/>(static/js, vanilla ESM)"]
        MCP_C["LLM agents"]
    end

    subgraph app["FastAPI process (main.py lifespan)"]
        direction TB
        ROUTES["api/routes/*<br/>folders · sync · files · jobs<br/>admin · auth · search"]
        WS["api/ws.py<br/>websocket pump"]
        MCPSRV["mcp_server.py"]
        EVENTS["services/events.py<br/>topic pub/sub + coalescing"]

        subgraph pipeline["Ingestion pipeline"]
            WATCH["watcher.py<br/>(watchdog, debounced)"]
            SCAN["scanner.py<br/>(startup reconcile)"]
            JQ["job_queue.py<br/>SQLite queue + dedup"]
            WORK["worker.py<br/>async pool (size=1)"]
            IDX["indexing.py<br/>extract→chunk→embed"]
        end

        subgraph svc["Services"]
            PARSE["parsers/registry"]
            CHUNK["chunking/registry"]
            EMB["embedding/factory<br/>text · image · sparse"]
            SYNC["sync/*<br/>gdrive · sharepoint · teams<br/>github · nfs"]
        end
    end

    subgraph stores["Stores"]
        CAS[("CAS on disk<br/>cas/files · cas/images")]
        DB[("SQLite<br/>metadata only")]
        QD[("Qdrant<br/>chunks + images")]
    end

    SPA -->|HTTP| ROUTES
    SPA <-->|WebSocket| WS
    MCP_C --> MCPSRV

    WATCH --> JQ
    SCAN --> JQ
    SYNC -->|pull files to disk| WATCH
    ROUTES -->|enqueue sync / reindex| JQ
    JQ --> WORK --> IDX
    IDX --> PARSE --> CHUNK --> EMB
    IDX --> CAS
    IDX --> DB
    EMB --> QD

    WORK -->|publish job.*| EVENTS
    IDX -->|publish file.* / folder.*| EVENTS
    EVENTS --> WS
    ROUTES --> DB
    MCPSRV --> QD
    MCPSRV --> DB
```

**Key invariants** (from the code's own module docstrings):

- Re-indexing is **whole-file** — any change resets `state='pending'` and
  re-runs the full extract → chunk → embed pipeline against the new bytes.
- **SQLite stores metadata only.** Extracted text and image bytes live in
  content-addressed `cas/` blobs (refcounted, GC-swept by `cas/gc.py`).
- **Two Qdrant collections**: `chunks` (dense e5-base-v2 + sparse BM25,
  RRF-fused) and `images` (SigLIP-2, searchable by text *or* image). Each
  point carries an `allowed_users` payload for the ACL filter.

---

## 2. File ingestion pipeline

The full lifecycle from a filesystem change to a searchable file. The watcher
debounces, enqueues an `extract` job, the worker claims it, and `indexing.py`
runs every stage under `_EXTRACT_LOCK`. Text and image embeds run **inline**
within the same extract job (not as separate queued jobs).

```mermaid
sequenceDiagram
    autonumber
    participant FS as Filesystem
    participant W as watcher.py
    participant Q as job_queue (SQLite)
    participant WK as worker.py
    participant IX as indexing.run_extract
    participant P as parser
    participant C as chunking
    participant CAS as CAS (disk)
    participant E as embedders (GPU)
    participant QD as Qdrant
    participant EV as events → SPA

    FS->>W: file created / modified
    W->>W: _Debouncer coalesce 0.5s<br/>key = folder_id:rel_path
    W->>Q: upsert File (state=pending)
    W->>Q: enqueue("extract", {file_id}, dedup_key="extract:id")
    W->>EV: publish file.upserted (shows pending file)

    WK->>Q: claim_one() → state queued→running, attempts++
    WK->>EV: publish job.started
    WK->>IX: run_extract({file_id})

    Note over IX: acquire _EXTRACT_LOCK (process-wide)
    IX->>IX: resolve path · stat (size cap) · read bytes · sha256
    alt sha matches previous extract
        IX->>IX: short-circuit (no-op + state heal)
    else changed
        IX->>P: parse → markdown + images + page renders + layout
        IX->>CAS: write text.md, image blobs, page renders, layout JSON
        IX->>C: chunk(markdown) → ChunkInfo[]
        IX->>IX: commit_indexing (txn):<br/>replace chunks/images, CAS refcounts,<br/>ChunkImageLink within nearby radius
        Note over IX: File → extracted; pending_embeds = (1 if chunks) + n_images
        IX->>E: embed text (dense e5 + sparse BM25) [gpu_lock]
        IX->>QD: replace_chunks_for_file (batches of 256)
        IX->>E: embed each image (SigLIP-2) [gpu_lock]
        IX->>QD: upsert image points (CAS-dedup via file_ids[])
        IX->>IX: decrement pending_embeds → 0 ⇒ File = indexed
    end
    Note over IX: release _EXTRACT_LOCK

    WK->>Q: mark_done()
    WK->>EV: publish job.finished + folder.stats_changed
```

### Pipeline stages inside `indexing.py`

Most stages are wrapped in a `_stage()` context manager for timing/logging
(the unchanged-short-circuit check is an early return, not a wrapped stage):

| # | Stage | `_stage()`? | What it does |
|---|-------|:---:|--------------|
| 1 | `resolve_path` | ✓ | File + Folder rows → absolute path |
| 2 | `stat` | ✓ | Existence + size `< max_file_bytes` (indexing cap) |
| 3 | `read_bytes` | ✓ | Read full file into memory |
| 4 | `sha256` | ✓ | Hash → `file_cas_id` |
| — | short-circuit unchanged | — | Early return (no-op) if sha matches prior extract, after healing orphaned states |
| 5 | `find_parser` | ✓ | Registry lookup by extension; else → `unsupported` |
| 6 | `parse` | ✓ | `ParseResult`: markdown, figures, page renders, layout, `char_to_page` |
| 7 | `cas_write_*` | ✓ | Several stages: `cas_write_text`, `cas_write_images`, `cas_write_page_images`, `cas_write_page_layout`, `cas_write_char_to_page`, `cas_write_manifest`, … → `cas/files/<sha>/...` and `cas/images/<sha>.bin` |
| 8 | `chunk` | ✓ | Chunking registry → `ChunkInfo[]` |
| 9 | `commit_indexing` | ✓ | Txn: replace chunks/images, refcounts, `ChunkImageLink` |
| 10 | `embed_text_inline` | ✓ | Dense + sparse vectors → `chunks` collection (if any chunks) |
| 11 | `embed_image_inline` | ✓ | SigLIP-2 vectors → `images` collection, per-image (failures non-fatal) |

**Image ↔ chunk linkage:** every extracted image gets an *anchor chunk* (the
chunk straddling its position in the markdown). Chunks within
`chunk_image_link_radius` get a `nearby_image` link, with chunk-index distance
stored as the score (`chunk_image_links.distance`).

**Image dedup:** image points carry a `file_ids` **array**. If the same
`image_cas_id` was already embedded for another file, the existing point is
reused and the new `file_id` is appended instead of re-embedding.

---

## 3. Job queue mechanics

A SQLite-backed queue with per-key in-flight deduplication. No automatic retry
loop — failure is terminal; the operator requeues via a folder reindex.

```mermaid
flowchart LR
    subgraph producers["Producers"]
        Wv["watcher"]
        Sc["scanner"]
        Rx["REST /reindex"]
        Sy["REST /sync"]
        Gc["scheduler"]
    end

    Wv -->|extract / delete_file| ENQ
    Sc -->|extract| ENQ
    Rx -->|reindex_folder| ENQ
    Sy -->|sync| ENQ
    Gc -->|gc_cas| ENQ

    ENQ{{"enqueue()"}}
    ENQ -->|dedup_key in-flight?| DEDUP{existing<br/>queued/running?}
    DEDUP -->|yes| REUSE["return existing id<br/>(no new row)"]
    DEDUP -->|no| ROW["insert Job<br/>state=queued"]
    ROW --> FA["folder_active.on_enqueued<br/>→ folder.active_changed"]

    ROW --> CLAIM["claim_one()<br/>queued→running, attempts++"]
    CLAIM --> RUN["handler runs"]
    RUN -->|ok| DONE["mark_done → done"]
    RUN -->|raise| ERR["mark_error → error"]
    DONE --> FAF["folder_active.on_finished"]
    ERR --> FAF
```

### Job kinds

| Kind | Payload | Producer | Handler |
|------|---------|----------|---------|
| `extract` | `{file_id}` | watcher, scanner, reconcile | `run_extract` |
| `embed_text` | `{file_id, round}` | (now inline) | `run_embed_text` |
| `embed_image` | `{image_id, round}` | (now inline) | `run_embed_image` |
| `delete_file` | `{file_id}` | watcher (on delete) | `run_delete_file` |
| `sync` | `{folder_id}` | REST `/folders/{id}/sync` | `run_sync` |
| `reindex_folder` | `{folder_id, file_ids}` | REST `/folders/{id}/reindex` | `run_reindex_folder` |
| `gc_cas` | `{}` | scheduler | (CAS GC sweep) |

- **Dedup key** (e.g. `extract:42`) guarantees at most one in-flight job per
  resource. A duplicate enqueue returns the existing id and does **not** bump
  the folder-active counter.
- **Attempts** increments on each `claim_one`. On process restart, any rows
  left `running` are swept to `error` ("abandoned").

---

## 4. File & job state machines

```mermaid
stateDiagram-v2
    direction LR
    [*] --> pending: watcher/scanner sees file
    pending --> extracted: commit_indexing (has chunks/images)
    pending --> indexed: commit_indexing (nothing to embed)
    pending --> unsupported: no parser / size cap
    extracted --> embedding: embed stage starts
    embedding --> indexed: pending_embeds → 0
    pending --> error: any stage raises
    extracted --> error
    embedding --> error
    indexed --> pending: file changed (whole-file reindex)
    error --> pending: reindex
    pending --> deleted: file vanished
    indexed --> deleted
    deleted --> [*]
```

```mermaid
stateDiagram-v2
    direction LR
    [*] --> queued: enqueue()
    queued --> running: claim_one()
    running --> done: mark_done()
    running --> error: mark_error()
    running --> error: process restart (abandoned)
    done --> [*]
    error --> [*]
```

---

## 5. Event system & websocket propagation

The WebSocket is the **single source of truth** for client state. Every
server→client state update — live data *and* every modal's state — flows
through one channel: an authenticated handshake, a full **snapshot** on
connect, then coalesced **deltas**. Mutations are still HTTP `POST`/`PATCH`
(the command path); the UI never refetches — it waits for the WS echo. This is
what makes reconnect bulletproof: a dropped socket re-snapshots on reconnect,
so a client that missed events converges back to server truth with **no page
reload** and no HTTP fallback.

`services/events.py` is an in-process topic broker. Publishers (any thread)
call `events.publish(topic, event)`; per-connection `Subscription` inboxes
buffer and **coalesce** by `(type, id)` so the client sees only the latest
state per resource. `api/ws.py` authenticates, sends the snapshot, then drains
the buffer in batches — filtering every batch per-connection by ACL.

### Connection lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant C as Browser (ws.js)
    participant W as api/ws.py
    participant A as deps.resolve_ws_user
    participant SB as api/snapshot.py
    participant EV as events.Subscription

    C->>W: WS connect (session cookie)
    W->>A: resolve_ws_user(ws.session)
    alt not signed in (multi-user)
        A-->>W: None
        W-->>C: close 4401 → ws.js reloads to sign-in
    else authenticated
        A-->>W: (user, is_admin)
        C->>W: {type:subscribe, topics}
        W-->>C: {type:subscribed}
        W->>EV: attach Subscription(user_id, is_admin, visible)
        W->>SB: build_snapshot (ACL-scoped, off-thread)
        SB-->>C: snapshot frames (folders/active/files/jobs[/admin/keys])
        W-->>C: {type:synced}  → pill goes green
        loop until disconnect
            W->>EV: refresh visible set if acl_version moved
            EV-->>W: drain(512), filter by ACL + topic plane
            W-->>C: single event or {type:batch, events:[…]}
        end
    end
```

### Per-connection delivery filter (`ws.py:_deliverable`)

Three scoping planes, applied to every drained batch:

```mermaid
flowchart TB
    E["event"] --> T{"type prefix?"}
    T -->|admin.*| AD{"sub.is_admin?"}
    AD -->|yes| OK["deliver"]
    AD -->|no| DROP["drop"]
    T -->|keys.*| KE{"event.user_id == sub.user_id?"}
    KE -->|yes| OK
    KE -->|no| DROP
    T -->|folder-scoped| FL{"folder_id in visible<br/>(union pre-refresh set)?"}
    FL -->|"yes / no folder"| OK
    FL -->|no| DROP
```

- **Folder ACL:** `_event_folder_id(event)` ∈ the connection's cached
  `visible` set (owned + granted + shared). Admin / single-user have
  `visible = None` → no folder filter. Removals filter against the **union**
  of the pre- and post-refresh visible sets so `folder.removed` /
  `file.deleted` for a folder you *could* see still arrive (a folder you never
  could see stays filtered — no leak).
- **ACL freshness:** folder add/remove/share/grant/revoke bump a global
  `acl_version`; the pump recomputes `visible` off-thread on the next tick.
- **admin plane:** `admin.*` delivered only to admin connections.
- **keys plane:** `keys.*` delivered only to the owning user (even for admins —
  keys are personal).

### Topics & events

| Topic | Events | Scope · coalesced by |
|-------|--------|--------------|
| `files` | `file.upserted` (carries `folder_id`) | folder · `file.id` |
| `files` | `file.deleted` (enriched with `folder_id`) | folder · discrete |
| `jobs` | `job.started`, `job.finished` (enriched with `folder_id`) | folder · `job_id` |
| `folders` | `folder.upserted`, `folder.stats_changed`, `folder.sync_source_changed`, `folder.active_changed` | folder · `folder_id` |
| `folders` | `folder.added`, `folder.removed`, `folder.sync_progress`, `folder.reindex_progress`, `folder.sync_config_changed`, `folder.gd_connected`, `folder.ms_connected` | folder · discrete |
| `admin` | `admin.snapshot` (full admin-console state) | admin-only · discrete |
| `keys` | `keys.snapshot` (a user's API keys) | per-user · discrete |
| `stats` | (reserved) | — |

Snapshot frames sent on connect: `{type:"snapshot", topic, items}` for
folders/active/files/jobs, plus `admin.snapshot` (admins) and `keys.snapshot`,
terminated by `{type:"synced"}`. The same `admin.snapshot` / `keys.snapshot` /
`folder.sync_config_changed` frames are re-published on the matching mutation,
so the client applies one shape whether it's the baseline or a delta.

**Backpressure:** when the buffer exceeds capacity (16384), the oldest
*coalesced* entry is evicted (the newest snapshot is always preserved). Under
heavy indexing, tens of thousands of `file.upserted` events collapse to one
final state per file id, and the pump emits one `send_text` per scheduling
tick.

> **Sync config note:** the heavy, secret-masked per-folder connector config is
> *not* in the global snapshot. `folder.sync_config_changed` keeps an open sync
> modal live (across tabs, and after save); the modal lazy-loads a folder's
> config once via HTTP if it isn't cached yet.

---

## 6. Search query path

`POST /api/search` embeds the query three ways, resolves the visible-folder
ACL filter, then queries both Qdrant collections and RRF-fuses the chunk
results.

```mermaid
sequenceDiagram
    autonumber
    participant U as Client
    participant S as routes/search.py
    participant ACL as ACL (visible_folder_ids)
    participant E as embedders
    participant QD as vector_store → Qdrant

    U->>S: POST /api/search {query, modes, folder_ids, limit}
    S->>ACL: _resolve_folder_ids(user, requested)
    Note over ACL: multi-user: intersect requested ∩ visible<br/>empty ⇒ [-1] (Qdrant short-circuit)
    S->>E: embed_query → dense (e5) + sparse (BM25)
    S->>E: embed_text → image vector (SigLIP-2)
    par chunks
        S->>QD: search_chunks(dense, sparse, folder_ids)
        QD->>QD: dense query + sparse query
        QD->>QD: RRF fuse (k=60)
    and images
        S->>QD: search_images(vector, folder_ids)
    end
    QD-->>S: hits (payload incl. folder_id, allowed_users, page, layout)
    S-->>U: SearchOut {chunks:[Hit], images:[Hit]}
```

**ACL model:** access control is enforced **at the folder level, in Python**,
before the Qdrant call. `_resolve_folder_ids` intersects the caller's requested
`folder_ids` with their *visible* set (`visible_folder_ids` — owned + ACL-granted
+ shared), so a request can't reach into another user's folder; an empty
intersection becomes `[-1]` (an impossible id) to give Qdrant a cheap no-match
path. The resulting list is passed to Qdrant as the `folder_ids` filter. In
single-user mode the filter is skipped entirely (`None`).

> Note: point payloads do carry an `allowed_users` array, but the `/api/search`
> endpoint does **not** apply a per-user `MatchValue` filter inside Qdrant — ACL
> rests on the folder-id intersection above. (`search_chunks` / `search_images`
> accept an optional `allowed_user_id`, but the REST endpoint doesn't pass it.)

---

## 7. Admin settings: storage & propagation

Two things to keep separate here:

- **Backend storage & read path** — still file/DB, lazy, no cache (unchanged).
  The consumers (`is_email_allowed`, `get_caps`, `get_nfs_root`, the
  `admin_user` dep) re-read their backing store on each use.
- **Client propagation** — now **WS-pushed**, not pull-based. After a mutation,
  `routes/admin.py` calls `publish_admin_state()`, which rebuilds the full
  admin state and emits an `admin.snapshot` on the **admin-only** `admin` topic.
  The admin modal renders from the `adminState` store — no GET on open, no
  refetch after mutation — so a change in one admin's tab shows up live in
  another's. (Previously this was `refreshAdmin()` pull-on-open + re-GET.)

```mermaid
flowchart TB
    subgraph ui["Admin UI (modals/admin.js)"]
        SUB["renders from adminState store<br/>(WS admin.snapshot) — no HTTP on open"]
    end

    subgraph routes["routes/admin.py — mutations gated by admin_user (403 else)"]
        R1["allowlist domains/users/blocklist"]
        R2["PATCH users/{id} is_admin"]
        R3["auth-providers CRUD + /check"]
        R4["indexing-caps GET/PATCH"]
        R5["settings GET/PATCH (nfs_root)"]
        PUB["publish_admin_state()<br/>→ admin.snapshot (admin topic)"]
    end

    subgraph store["Persistence (services/admin_store.py + others)"]
        F1["allowed_domains.txt<br/>allowed_users.txt<br/>blocked_users.txt"]
        F2["settings.json (nfs_root)"]
        F3["indexing_caps.json"]
        F4[("SQLite: users.is_admin")]
        F5[("SQLite: auth_providers")]
    end

    subgraph read["Backend read path (lazy, NO cache)"]
        C1["is_email_allowed() — at sign-in"]
        C2["get_nfs_root() — re-probed every browse/sync"]
        C3["get_caps() — re-reads disk every call"]
        C4["admin_user dep — DB read per request"]
        C5["auth-provider rows — read at login / sync config"]
    end

    R1 --> F1 --> C1
    R5 --> F2 --> C2
    R4 --> F3 --> C3
    R2 --> F4 --> C4
    R3 --> F5 --> C5
    R1 & R2 & R3 & R4 & R5 --> PUB
    PUB -->|WS, admins only| SUB
```

### What lives where

| Setting | Persistence | Read path | Invalidation |
|---------|-------------|-----------|--------------|
| Allowed domains / users / blocklist | plain `.txt` files (`<data>/admin/`) | `is_email_allowed()` | takes effect at next sign-in |
| `is_admin` flag | SQLite `users.is_admin` | `admin_user` dep, per request | next request; super-admins re-stamped each login |
| Auth providers (OAuth catalog) | SQLite `auth_providers` | read at login / sync-config time | next login or restart (env rows re-seed) |
| NFS root | `settings.json` | `get_nfs_root()` | re-probed on every browse/sync call |
| Indexing caps | `indexing_caps.json` | `get_caps()` — **always re-reads disk** | every call (no cache) |
| API keys (per-user, *not* admin) | SQLite `api_keys` | per-user, on demand | re-fetched on demand |

### Propagation properties

- **Backend reads: no caching.** Every read hits the file/DB fresh. A comment
  in `indexing_caps.py` notes that *cross-process* invalidation would require a
  pubsub channel — fine for the single-process default deployment.
- **Client: WS-pushed.** The admin modal renders from the `adminState` store,
  fed by `admin.snapshot` (on connect to admins, and re-pushed after every
  mutation). No pull-on-open, no post-mutation refetch. A focus-guard skips the
  re-render while the admin is editing an input so a concurrent push can't
  clobber in-progress typing.
- **API keys, likewise.** `modals/settings.js` renders from the `keysState`
  store (per-user `keys.snapshot`), pushed after each create/delete.
- **Atomic writes.** Text/JSON files are written to `.tmp` then `os.replace()`d.
- **Admin vs per-user settings are distinct UIs and WS planes.**
  `modals/admin.js` (admin-only `admin` topic) edits deployment-wide settings;
  `modals/settings.js` (per-user `keys` topic) edits only that user's API keys.

### Sequence: an admin changes an indexing cap

```mermaid
sequenceDiagram
    autonumber
    participant A as Admin (browser)
    participant API as routes/admin.py
    participant CAPS as indexing_caps.json
    participant WS as other admin tabs
    participant WK as worker (next extract)

    A->>API: PATCH /api/admin/indexing-caps {xlsx_max_rows: 100000}
    Note over API: Depends(admin_user) — 403 if not admin
    API->>CAPS: update(): drop unknown keys, clamp to BOUNDS,<br/>write .tmp → os.replace
    API->>WS: publish_admin_state() → admin.snapshot (admin topic)
    API-->>A: 200 {values, defaults, bounds}
    WS-->>A: admin.snapshot → adminState store re-renders caps tab
    WS-->>WS: every other admin's modal updates live too
    Note over WK: backend read path unchanged — no cache bust.
    WK->>CAPS: get_caps() re-reads disk on the very next extract
```

---

## 8. OAuth providers: admin defines → user consumes

There are **two separate provider mechanisms** — don't conflate them:

| | **Login auth providers** | **Sync connectors** |
|---|---|---|
| Table | `auth_providers` (global catalog) | `folder_sync_sources` (per-folder) |
| Managed by | admin, via Admin → OAuth tab | folder owner, via Sync modal |
| Purpose | sign-in identity (currently Google) | pulling content from Drive/SP/Teams/GitHub/NFS |
| Scopes | `openid email profile` | per-connector (e.g. `drive.readonly …`) |
| Tokens | session cookie | per-folder refresh token |

The intended design: the admin defines an OAuth app **once** in the
`auth_providers` catalog, and **every user** picks it as a shortcut in the
per-folder sync modal — the picker pre-fills `client_id` / `client_secret` so
the user doesn't have to register their own Google/Azure app.

> **Fixed bug (was admin-only).** `GET /api/admin/auth-providers` used to be
> gated by `admin_user`, so a non-admin opening the sync modal got a 403 and
> saw an **empty** provider picker — defeating the whole "shared shortcut"
> intent. The gate is now `current_user`: the list is readable by any
> authenticated user; only the mutating routes (POST/PATCH/DELETE/check) remain
> admin-only. The response includes `client_secret` by design — it's the shared
> app credential users are meant to use, and it lands in their folder's sync
> row regardless.

```mermaid
flowchart TB
    subgraph admin["Admin"]
        ADM["Admin → OAuth tab (modals/admin.js)"]
        ADM -->|POST/PATCH /api/admin/auth-providers| AP[("auth_providers<br/>provider · client_id<br/>client_secret · tenant_id<br/>enabled · source: env or user")]
        ENV[".env VOITTA_GOOGLE_AUTH_*"] -->|upsert_env_provider on startup<br/>source='env'| AP
        ADM -->|POST .../check| PROBE["probe token endpoint<br/>invalid_client vs invalid_grant"]
    end

    subgraph login["Login (separate path)"]
        LG["login.js: GET /api/auth/config<br/>{google_enabled}"]
        LG -->|if enabled show button| BTN["Sign in with Google"]
        BTN -->|GET /api/auth/login/google| OAUTH["Google consent<br/>scopes: openid email profile"]
        OAUTH -->|/api/auth/google/callback| GATE["is_email_allowed? →<br/>create User, set session cookie"]
    end

    subgraph syncpicker["Sync modal reuses the catalog (any authenticated user)"]
        SM["sync.js: refreshGdProviderPicker()"]
        SM -->|GET /api/admin/auth-providers<br/>current_user gate, read-only| AP
        SM -->|filter google + enabled| PICK["populate select dropdown<br/>pre-fill client_id/secret"]
        SM2["refreshMsProviderPickers()"]
        SM2 -->|filter microsoft + enabled| PICK
    end
```

### How a regular user *discovers* providers

- **Login button:** `login.js` calls `GET /api/auth/config`, which returns
  `{google_enabled}` derived from whether the `.env` Google client id/secret
  are set. (Login currently reads `.env` directly, not the `auth_providers`
  table — Microsoft/GitHub rows are stored but not yet wired to login.)
- **Sync picker:** `sync.js` calls `GET /api/admin/auth-providers` (readable by
  any signed-in user) and filters for `enabled` rows of the relevant provider.
  The matching rows populate the picker for **every** user; selecting one
  pre-fills the credentials into the per-folder form. Manual entry remains
  available if no catalog provider fits. (Previously this endpoint was
  admin-only, so non-admins saw an empty picker — see the fixed-bug note above.)

### Admin-defined OAuth provider lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant Admin
    participant API as routes/admin.py
    participant DB as auth_providers
    participant Probe as provider token endpoint
    participant User as Folder owner (sync modal)

    Admin->>API: POST /api/admin/auth-providers {provider:google, client_id, client_secret}
    Note over API: admin_user gate (write) · provider ∈ {google, microsoft, github}
    API->>DB: insert row (source='user', enabled=true)
    Admin->>API: POST .../{id}/check
    API->>Probe: exchange bogus code
    Probe-->>API: invalid_grant ⇒ creds OK · invalid_client ⇒ bad creds
    API-->>Admin: status badge

    User->>API: GET /api/admin/auth-providers (from sync.js)
    Note over API: current_user gate (read-only) — any signed-in user
    API-->>User: [enabled google rows incl. client_secret]
    Note over User: picker pre-fills client_id/secret<br/>into the per-folder sync form
```

---

## 9. Sync OAuth runtime flows

Once a folder's sync source has client credentials (typed or pre-filled from
the catalog), the per-folder OAuth dance runs in a popup. The `folder_id` is
carried through OAuth `state` (base64). On callback the **refresh token is
stored on the folder's sync-source row**, and a websocket event tells the
modal it can close.

```mermaid
sequenceDiagram
    autonumber
    participant U as User (sync modal popup)
    participant API as routes/sync.py
    participant DB as folder_sync_sources
    participant G as Google / Microsoft
    participant EV as events → SPA

    U->>API: POST /folders/{id}/sync/google-drive/auth
    API->>DB: read gd_client_id / gd_client_secret / gd_use_loopback
    API-->>U: {auth_url}  (state = base64(folder_id))
    U->>G: open consent (scopes: drive.readonly, documents.readonly, …)
    G-->>U: redirect to callback w/ code + state
    U->>API: GET /api/sync/oauth/google/callback?code&state
    API->>DB: decode state → folder; read creds
    API->>G: exchange code → tokens
    G-->>API: refresh_token
    API->>DB: store gd_refresh_token
    API->>EV: publish folder.gd_connected
    API-->>U: self-closing HTML
    EV-->>U: modal observes event, closes popup
```

**Redirect URIs** (two modes per provider):

- Standard: `{proto}://{host}/api/sync/oauth/{google|microsoft}/callback`
  (proto/host from `X-Forwarded-*` headers).
- Loopback: fixed `http://localhost:53682/api/sync/oauth/{…}/callback`
  (toggle `*_use_loopback`; the modal shows which URI is active).

**Microsoft specifics:** the same flow targets
`login.microsoftonline.com/{tenant_id}/oauth2/v2.0/{authorize,token}` with
delegated scopes (`offline_access Sites.Read.All Files.Read.All …`) and handles
the admin-consent redirect. Microsoft **rotates** refresh tokens on most
refreshes — connectors persist the new token whenever
`auth.rotated_refresh_token` is set. SharePoint and Teams share the same `ms_*`
credential fields.

### Connector matrix

| Provider | `source_type` | Auth methods | Token storage |
|----------|---------------|--------------|---------------|
| Google Drive | `google_drive` | OAuth · service-account JSON | `gd_refresh_token` (per folder) |
| GitHub | `github` | SSH key · PAT · none | `gh_token` / `gh_pat` (per folder) |
| SharePoint | `sharepoint` | OAuth · app-secret · app-cert | `ms_refresh_token` (OAuth) |
| Teams | `teams` | OAuth · app-secret · app-cert | `ms_refresh_token` (OAuth) |
| NFS | `nfs` | none (path under admin `nfs_root`) | — |

Connectors are registered in `services/sources/registry.py` /
`services/sync/__init__.py` and resolved by `get_connector(source_type)`.

---

## 10. Data model

SQLite holds **metadata only**. Content lives in CAS; vectors live in Qdrant.

```mermaid
erDiagram
    users ||--o{ folders : owns
    users ||--o{ api_keys : has
    folders ||--o{ files : contains
    folders ||--o| folder_sync_sources : "0..1 sync source"
    folders ||--o{ folder_acl : grants
    folders ||--o{ folder_user_settings : "per-user active flag"
    files ||--o{ chunks : has
    files ||--o{ images : has
    files ||--o{ file_acl : grants
    chunks ||--o{ chunk_image_links : near
    images ||--o{ chunk_image_links : near
    images }o--|| chunks : "anchor_chunk"

    users {
        int id PK
        string email
        bool is_admin
    }
    folders {
        int id PK
        string path
        int owner_id FK
        bool shared
        bool enabled
    }
    files {
        int id PK
        int folder_id FK
        string rel_path
        string file_cas_id
        string state
        int pending_embeds
        int embed_round
        string error
    }
    chunks {
        int id PK
        int file_id FK
        int chunk_index
        text text
        int char_start
        int char_end
    }
    images {
        int id PK
        int file_id FK
        int image_index
        string image_cas_id
        int anchor_chunk
        int page
        string kind
    }
    chunk_image_links {
        int chunk_id FK
        int image_id FK
        int distance
    }
    cas_refs {
        string cas_id PK
        string kind PK
        int refcount
        float last_decref_at
    }
    jobs {
        int id PK
        string kind
        json payload
        string state
        int attempts
        string dedup_key
        string error
    }
    auth_providers {
        int id PK
        string provider
        string client_id
        string client_secret
        string tenant_id
        bool enabled
        string source
    }
    folder_sync_sources {
        int folder_id PK
        string source_type
        string gd_refresh_token
        string ms_refresh_token
        string gh_pat
        json gd_folder_id
        bool auto_sync_enabled
        int auto_sync_hours
        string sync_status
    }
```

### CAS layout on disk

```
cas/
├── files/<file_sha>/
│   ├── text.md                 parsed markdown
│   ├── page_layout.json        per-page block layout (if parser emits)
│   ├── layout_summaries.json   per-page indexed summary scalars
│   ├── char_to_page.json       char offset → page number
│   ├── on_demand_assets.json   LLM-callable asset menu
│   └── manifest.json           parser name, chunk/image counts
└── images/<image_sha>.bin      raw image bytes
```

`cas_refs(cas_id, kind, refcount, last_decref_at)` tracks delete-readiness; the
`gc_cas` job sweeps blobs whose refcount has been zero for a quiet period.

---

## 11. Locking model

Three coordination primitives keep the C-level libraries, the GPU, and
QdrantLocal's thread-pinned SQLite connection safe.

```mermaid
flowchart TB
    subgraph extract["_EXTRACT_LOCK (threading.Lock)"]
        direction TB
        EX["run_extract pipeline<br/>(PyMuPDF/cairo/Pillow not thread-safe)"]
        WIPE["wipe_file_data() from /reindex<br/>must not race in-flight extract"]
    end

    subgraph gpu["gpu_lock (threading.Lock)"]
        direction TB
        MIN["MinerU PDF parse"]
        SIG["SigLIP image+text embed"]
        E5["e5 dense text embed"]
        SRCH["search query embed<br/>(serialized vs indexing)"]
    end

    subgraph qd["Qdrant single worker thread"]
        direction TB
        QW["ThreadPoolExecutor(max_workers=1)<br/>run_on_qdrant(fn)<br/>QdrantLocal SQLite is thread-pinned"]
    end

    EX -.serializes vs.-> WIPE
    MIN -.serializes.-> SIG -.serializes.-> E5 -.serializes.-> SRCH
```

| Lock | Protects | Why |
|------|----------|-----|
| `_EXTRACT_LOCK` | the whole extract pipeline + `wipe_file_data` | PyMuPDF/cairo/Pillow C decoders corrupt the heap under parallelism; also prevents a `/reindex` wipe (REST thread) from racing an in-flight extract (worker thread) |
| `gpu_lock` | every model inference (MinerU, SigLIP, e5) | serializes GPU work, including search-query embeds vs. indexing, so they don't collide on the device |
| Qdrant worker thread | all Qdrant I/O | QdrantLocal's SQLite connection is pinned to its creating thread; routing every call through one worker keeps it thread-safe |

The default worker pool size is **1**, so two workers can't collide in the
pipeline even without `_EXTRACT_LOCK` — but the lock remains necessary for the
REST-thread/worker-thread reindex race.

---

*Generated from a source trace of `src/voitta_rag_enterprise/` and `static/js/`.
Line-level references were accurate at the time of writing; treat file paths as
the durable anchors and re-verify specifics against the code.*
