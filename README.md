# voitta-image-rag

Filesystem-driven RAG with first-class image support, content-addressable extraction, real-time websocket UI, and a pluggable sync layer.

**Design docs:** [ARCHITECTURE.md](./ARCHITECTURE.md) · [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)

## TL;DR

- Register a folder. The watcher picks up changes; a job queue extracts text + images into a content-addressable store on disk; embeddings land in Qdrant.
- **Two Qdrant collections:** `chunks` (e5-base-v2 dense + Qdrant BM25 sparse, RRF-fused) and `images` (SigLIP-2 image vector — searchable by text *or* image).
- **Image ↔ chunk linkage:** every image carries an anchor chunk; chunks within radius `N` get a `nearby_image` link with chunk-index distance.
- **No manual sync/embed buttons.** Re-indexing is whole-file on any change.
- **SQLite stores metadata only.** Extracted text and images live in `cas/<sha>/...`.
- **Multi-user with ACLs.** `VOITTA_SINGLE_USER=true` collapses to a `root` user.
- **Frontend** is a vanilla ES-modules SPA over a WebSocket event stream.
- **MCP server** exposes the same data to LLM agents.

## Quickstart

```bash
# 1. Install (defaults to fake embedders — no model downloads required)
make install

# 2. Sanity-check the environment
VOITTA_USE_FAKE_EMBEDDERS=true VOITTA_DEV_USER=you@localhost make doctor

# 3. Run the web app + workers
VOITTA_USE_FAKE_EMBEDDERS=true VOITTA_DEV_USER=you@localhost make dev
```

Then in another terminal:

```bash
# Register a folder
curl -X POST http://localhost:8000/api/folders \
  -H 'Content-Type: application/json' \
  -H 'X-Forwarded-Email: you@localhost' \
  -d '{"path": "/path/to/some/folder"}'

# Drop a file into that folder — the watcher picks it up automatically.
# Within seconds /api/files/<id> reports state=indexed.

# Search
curl -X POST http://localhost:8000/api/search \
  -H 'Content-Type: application/json' \
  -H 'X-Forwarded-Email: you@localhost' \
  -d '{"query": "hello world", "modes": ["chunks", "images"]}'
```

Open `http://localhost:8000/` for the SPA (folder list + live job feed + search).

## Real models

Fake embedders are deterministic (hash-based) and good for development; for real RAG quality install the `[ml]` extra:

```bash
pip install -e ".[dev,ml]"
unset VOITTA_USE_FAKE_EMBEDDERS
make dev
```

The dense (`intfloat/e5-base-v2`), sparse (`Qdrant/bm25`), and image (`google/siglip2-base-patch16-224`) models download lazily on first use.

## MCP server

```bash
make mcp                    # listens on $VOITTA_MCP_PORT (8001)
```

Exposes 8 tools: `search`, `search_images`, `get_file`, `get_chunk_range`, `get_chunk_images`, `get_image`, `list_indexed_folders`, `resolve_url`. ACL identity comes from the `X-User-Name` header.

## Common ops

```bash
make doctor                 # config + health probe
make seed-users             # import users.txt
make rebuild-index          # nuke CAS+Qdrant, re-extract every file
make reembed                # after a model upgrade: re-enqueue stale embed jobs
make test
make lint
```

## Configuration

All settings carry the `VOITTA_` env-var prefix. See [.env.example](./.env.example) for the full list and [ARCHITECTURE.md §11](./ARCHITECTURE.md#11-configuration) for semantics. The most useful for local dev:

| Var                          | Purpose                                                       |
|------------------------------|---------------------------------------------------------------|
| `VOITTA_DATA_DIR`            | Where SQLite + CAS + Qdrant live (default `~/.voitta-image-rag`) |
| `VOITTA_USE_FAKE_EMBEDDERS`  | Skip real model loads — recommended for dev                   |
| `VOITTA_SINGLE_USER`         | Collapse to a `root` user (no ACL filtering)                  |
| `VOITTA_DEV_USER`            | Authenticate every request as this email (no proxy needed)    |
| `VOITTA_DISABLE_BACKGROUND`  | Skip watcher + workers (useful for tests)                     |

## Status

All eight implementation stages are complete. 200+ tests; CI runs lint + tests on every PR.
