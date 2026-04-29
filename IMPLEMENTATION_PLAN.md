# voitta-image-rag — Implementation Plan

Stages are end-to-end vertical slices. Each one ships a working app at increasing capability. Order matters: every stage builds on the previous and leaves the system in a runnable state.

Effort estimates are rough and assume one engineer, full days. They're for sequencing intuition, not commitments.

---

## Stage 0 — Project skeleton (~0.5d)

**Outcome:** repo runs `make install && make run`, app boots, returns "ok" on `/healthz`, no features.

- `pyproject.toml` (uv-friendly), `Makefile` (`install`, `run`, `test`, `ui-dev`, `ui-build`).
- `src/voitta_image_rag/main.py`: FastAPI app factory + lifespan; mount static dir; `/healthz`.
- `config.py`: env-driven settings (Section 11 in ARCHITECTURE).
- `.env.example`, `.gitignore` (data dir, dist, `__pycache__`, `.venv`).
- `static/index.html` placeholder.
- `tests/conftest.py` with an `app_client` fixture.

**Done when:** `curl localhost:8000/healthz` returns `{"ok": true}`.

---

## Stage 1 — SQLite + folder registration + reconciliation scan (~1d)

**Outcome:** can register a folder via REST; on startup the app scans it and records every file in SQLite.

- `db/schema.sql` + SQLAlchemy models for `users`, `folders`, `files`, `cas_refs`, `jobs` (rest stubbed). Schema includes `files.source_url`, `files.pending_embeds`, and the `idx_jobs_dedup_inflight` partial unique index.
- `db/database.py`: engine init, WAL mode, migrations via simple `init_db()` that runs `schema.sql` if tables missing. v1 = no schema upgrades; rebuild if you change the schema.
- `services/acl.py`: `current_user(request)` resolver supporting all three modes from ARCHITECTURE §9 (`VOITTA_SINGLE_USER`, `VOITTA_DEV_USER`, reverse-proxy header). Reject requests with no resolvable user in multi-user mode.
- `services/ignore.py`: parse `VOITTA_IGNORE_PATTERNS` into a glob matcher; used by scanner and watcher.
- `api/routes/folders.py`: `POST /api/folders`, `GET /api/folders`, `DELETE /api/folders/{id}`.
- `services/scanner.py`: `scan_folder(folder)` — walk (skipping ignored patterns), upsert `files` rows (state=`pending`), mark missing as `deleted`. Reads `.voitta_sources.json` sidecar at folder root and populates `files.source_url`. Run on startup for every enabled folder.
- Tests: register a folder pointing at a temp dir with three files → `GET /api/folders/{id}/files` lists them; ignored patterns excluded; sidecar URLs surfaced.

**Done when:** `POST /api/folders {path: "/some/dir"}` registers, restart shows files indexed in DB.

---

## Stage 2 — Watcher + job queue + content-addressable storage (~1.5d)

**Outcome:** changing a file on disk triggers a job; CAS hashing works; CAS GC sweeps unreferenced blobs.

- `services/watcher.py`: `watchdog` Observer per folder; debounce ~500ms per path; honour `VOITTA_IGNORE_PATTERNS`; skip files larger than `VOITTA_MAX_FILE_BYTES`. Emit intents.
- `services/job_queue.py`:
  - `enqueue(kind, payload, *, dedup_key=None, priority=0)` — catches the unique-violation on `idx_jobs_dedup_inflight` and returns the existing in-flight job's id (no-op coalescing).
  - `claim_one()`, `mark_done(job_id)`, `mark_error(job_id, msg)` — `BEGIN IMMEDIATE`. `mark_done` for `embed_*` jobs also decrements `files.pending_embeds` and flips `state='indexed'` when it hits zero, in the same transaction.
- `services/worker.py`: N async workers; main loop polls queue. Empty handlers for each `kind` (logs only).
- `cas/store.py`: two namespaces (`files/<sha>/` and `images/<sha>.bin`). API: `write_file_blob(file_sha, name, bytes)`, `read_file_blob(file_sha, name)`, `write_image_blob(bytes) -> image_sha`, `read_image_blob(image_sha)`, `incref(kind, sha)`, `decref(kind, sha)`.
- `cas/gc.py`: sweep `cas_refs.refcount=0` older than a quiet-period (default 60s); remove the corresponding `cas/files/<sha>/` dirs and `cas/images/<sha>.bin` files.
- `services/scanner.py`: enqueue `extract` (with `dedup_key="extract:<file_id>"`) for new/modified files; `delete_file` for vanished.
- `api/routes/files.py`: read-only `GET /api/files/{id}` and `GET /api/folders/{id}/files`.
- Tests:
  - `touch` a file in a watched folder → job appears within 1s.
  - Save the same file 5× in 100ms → only one in-flight `extract` job (dedup_key works).
  - Worker processes `delete_file`: refcounts decrement, GC sweeps after quiet period.
  - Ignored patterns and over-size files skipped by both scanner and watcher.

**Done when:** logs show `extract` and `delete_file` jobs flowing; CAS dirs exist (empty payloads OK).

---

## Stage 3 — Parsers + extraction → CAS + chunks/images metadata (~2.5d)

**Outcome:** real parsing. After `extract`, `text.md` and `images/*` exist in CAS, `chunks` and `images` rows populated, image-chunk linkage built.

- Carry `parsers/` from voitta-rag; refactor `BaseParser` to return `ParserResult(content, images, metadata, ...)` per ARCHITECTURE §4.4. Update PDF / DOCX / PPTX parsers to extract images:
  - PDF (PyMuPDF): `page.get_images()` → bytes + position from text-block ordering.
  - DOCX (python-docx): walk `document.part.related_parts` and inline shapes.
  - PPTX (python-pptx): per-slide shapes.
  - Text/markdown parsers: `images=[]`.
- New `image_parser.py`: handles `.jpg/.png/.webp/.heic/.tiff` — `content=""`, one `ExtractedImage`. Use Pillow to detect actual format and dimensions; store mime in DB, write blob as `<sha>.bin`.
- `services/chunking.py`: chunk markdown into ~512-token chunks with ~64-token overlap; record `(char_start, char_end)` per chunk. Carry voitta-rag's defaults.
- `services/indexing.py:run_extract(file_id)`:
  1. read bytes (skip if size > `VOITTA_MAX_FILE_BYTES`); compute `file_cas_id`; if equal to stored, bump `mtime_ns` only and exit (state stays whatever it was).
  2. select parser by extension via `parsers.registry`.
  3. parse → write `cas/files/<file_sha>/text.md` and `cas/files/<file_sha>/manifest.json`. For each image, write `cas/images/<image_sha>.bin` (skip if exists; `incref` either way).
  4. chunk markdown → upsert `chunks` rows.
  5. upsert `images` rows; compute `anchor_chunk` per image (with the inter-chunk-gap fallback from ARCHITECTURE §4.5); insert `chunk_image_links` for `|chunk_index - anchor| ≤ N`. Detect mime via Pillow before writing the SQLite row.
  6. set `files.state='extracted'`, `files.pending_embeds = (1 if any chunks else 0) + count(new images)`. Bump `last_indexed_at`.
  7. enqueue `embed_text(file_id)` (if any chunks) and one `embed_image(image_id)` per image (each with its own `dedup_key`). All enqueues in the same transaction as the counter set.
- Tests:
  - Parse a fixture PDF with two images on different pages → 2 image rows, correct anchor chunks, link rows present.
  - Image at exact chunk boundary → falls through to nearest-chunk fallback.
  - Re-extract same file unchanged → no churn (same `file_cas_id`, no CAS writes, no embed jobs).
  - Re-extract with content edit → old chunks/images deleted, new ones inserted, old image blobs decref'd, shared images survive.
  - Standalone `.png` file: zero chunks, one image, `pending_embeds=1`, single `embed_image` job.

**Done when:** `extract` runs end-to-end and CAS shows real markdown + image bytes; `pending_embeds` correctly tracks fan-out.

---

## Stage 4 — Embedding + Qdrant indexing (~2d)

**Outcome:** chunks are searchable via dense+sparse RRF; images are searchable cross-modally.

- `services/vector_store.py`: Qdrant client wrapper. Bootstrap two collections (`chunks`, `images`) with named vectors per ARCHITECTURE §3.4. Idempotent `ensure_collections()`.
- `services/embedding/text.py`: load e5-base-v2 (sentence-transformers); batched embed.
- `services/embedding/sparse.py`: Qdrant fastembed BM25.
- `services/embedding/image.py`: SigLIP-2 (transformers) — image encoder + text encoder. Lazy-load; one process-pool worker holds the model.
- Job handlers (each decrements `files.pending_embeds` on success in the same transaction; flips `state='indexed'` at zero):
  - `embed_text(file_id)`: load all chunks → batch embed dense + sparse → upsert to `chunks` collection (delete-then-upsert by `file_id` filter for atomicity). Stamp `dense_model_version` and `sparse_model_version` on every payload.
  - `embed_image(image_id)`: look up `image_cas_id`; check Qdrant for an existing point with that `image_cas_id`. If present, append this `file_id` to its `file_ids` array and skip embedding. If absent, load bytes from CAS → embed → upsert with `image_model_version` stamped. Idempotent.
- `api/routes/search.py`: `POST /api/search` — accepts `{query, modes: ['chunks','images'], filters, limit}`; runs both modalities; returns ranked hits with `nearby_*` payload included.
- Tests:
  - Index a folder with 5 PDFs and run a known-answer query → top-5 contains the right chunk.
  - Cross-modal: query "logo" → image with the project logo ranks high.
  - Reverse image: upload image bytes (multipart) → returns same image and visually similar ones.

**Done when:** `/api/search` returns useful results for both modes.

---

## Stage 5 — WebSocket events + minimal SPA (~2.5d)

**Outcome:** browser shows folders/files/jobs in real time. Search works in the UI.

- `services/events.py`: in-process broker. `publish(topic, event)` and per-connection `subscribe(topics)` queues. Indexing pipeline calls `publish` at every state transition.
- `api/ws.py`: `/ws` endpoint; receives `subscribe`/`search`/`cancel`; pushes events.
- `ui/` (Vite + Solid + TS):
  - `ws.ts`: connection manager with backoff reconnect.
  - Stores: `folders`, `files`, `jobs` (Solid stores), `search` (signal).
  - Components: `FolderList`, `FileTree`, `FileViewer`, `SearchPanel`, `JobMonitor`.
  - `FileViewer` rendering pipeline: fetch `/api/files/{id}/text` (raw markdown) and `/api/files/{id}/images` (list of `{image_id, position, page}`). Splice `<img src="/api/images/{image_id}/thumb">` tags into the markdown at each `position` in **descending order** so earlier offsets remain valid. Render via `marked` + `DOMPurify`.
  - Initial snapshot: on connect, fetch `/api/folders` + `/api/jobs/recent`, then `subscribe`.
- Build pipeline: `make ui-build` runs `vite build` → outputs to `static/dist/`. FastAPI serves them.
- Tests: a Playwright smoke test — register folder, drop a file in, see it appear in tree without refresh.

**Done when:** browser at `/` shows live folder/file/job state and search returns results.

---

## Stage 6 — MCP server (~1d)

**Outcome:** MCP tools functional, parity with voitta-rag where it matters, plus image tools.

- `mcp_server.py`: FastMCP setup on `:8001`. Pull voitta-rag's MCP module; adapt to new schema.
- Tools: `search`, `search_images`, `get_file`, `get_chunk_range`, `get_chunk_images`, `get_image`, `list_indexed_folders`, `resolve_url`.
- `X-User-Name` header → ACL filter applied to every tool.
- Tests: spin up MCP, call each tool against a seeded DB.

**Done when:** Voitta Desktop or `npx @modelcontextprotocol/inspector` can list and call all tools.

---

## Stage 7 — Multi-user wiring + sync plugin scaffold (~1d)

**Outcome:** ACLs enforced end-to-end; sync registry exists for future work.

- `services/acl.py`: folder-grant on register (default = creating user + everyone in `users.txt` if folder is "public"); per-file inherits from folder; admin tools to grant/revoke.
- Search filters: SQLite joins + Qdrant `allowed_users` payload filter, both required to agree.
- `services/sources/base.py` + `registry.py`: abstract `SyncConnector`, no concretes.
- `api/routes/users.py`: minimal CRUD + `/me`.
- Tests: two users, two folders with disjoint ACLs → searches return only allowed content.

**Done when:** `VOITTA_SINGLE_USER=true` and multi-user paths both green.

---

## Stage 8 — Polish & dev ergonomics (~1d)

- `scripts/rebuild_index.py`: nuke Qdrant + CAS + chunks/images rows, re-enqueue all `extract` jobs.
- `scripts/seed_users.py`: import `users.txt`.
- `make doctor`: prints config + sanity-checks model downloads + Qdrant reachability.
- README quickstart with screenshots.
- CI: `pytest`, `mypy`, `ruff`, `vite build`.

---

## Risk register

| Risk                                                    | Mitigation                                                                 |
|---------------------------------------------------------|----------------------------------------------------------------------------|
| Image-position offsets drift across parsers             | Parser-by-parser fixture tests asserting `(position, page)` on known docs |
| SigLIP / OpenCLIP model size / cold start               | Load lazily in a single worker; cache on disk; ship `make warmup`          |
| SQLite write contention under heavy watcher load        | WAL + single writer for jobs; readers unblocked; `BEGIN IMMEDIATE`         |
| Qdrant embedded data corruption on hard kill            | Use `VOITTA_QDRANT_URL` for production; embedded only for dev              |
| ACL/Qdrant/SQLite drift                                 | Always write Qdrant before SQLite for upserts; nightly `verify_acl_drift` |
| Reindex storm after rebuild                             | Job priorities — `extract` low, search-time `embed` boostable; rate-limit |
| CAS GC removing live blobs (race)                       | `decref` before `delete`; `gc_cas` only deletes after a quiet period       |
| Watcher misses events (esp. on macOS / network FS)      | Periodic reconciliation scan (every 10 min) closes the gap                 |

---

## Open questions to revisit during implementation

1. **Chunk size / overlap.** Inherit voitta-rag's defaults initially; tune once we have an eval set.
2. **Token budget for images returned with chunks.** A search hit could carry 0..N images; default to top-1 image by smallest `distance`, configurable.
3. **HEIC / RAW image support.** Likely Pillow + pillow-heif. Defer until a user actually has them.
4. **Telemetry.** OpenTelemetry hooks at job boundaries — out of scope v1, but leave a `tracing.py` stub.
5. **Re-embed migration script.** Architecture §3.4 commits to per-point `*_model_version`; the script that scans for stale versions and re-enqueues `embed_*` jobs is itself a Stage 8 deliverable, not yet specified here.
