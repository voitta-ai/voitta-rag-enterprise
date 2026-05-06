# Voitta RAG Enterprise

Filesystem-driven RAG with first-class image support, content-addressable extraction, real-time websocket UI, and a pluggable sync layer.

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
# Register a folder (created under $VOITTA_ROOT_PATH/<name>)
curl -X POST http://localhost:8000/api/folders \
  -H 'Content-Type: application/json' \
  -H 'X-Forwarded-Email: you@localhost' \
  -d '{"name": "project-x"}'

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

All settings carry the `VOITTA_` env-var prefix. See [.env.example](./.env.example) for the full list. The most useful for local dev:

| Var                          | Purpose                                                       |
|------------------------------|---------------------------------------------------------------|
| `VOITTA_DATA_DIR`            | Where SQLite + CAS + Qdrant live (default `~/.voitta-rag-enterprise`) |
| `VOITTA_USE_FAKE_EMBEDDERS`  | Skip real model loads — recommended for dev                   |
| `VOITTA_SINGLE_USER`         | Collapse to a `root` user (no ACL filtering)                  |
| `VOITTA_DEV_USER`            | Authenticate every request as this email (no proxy needed)    |
| `VOITTA_DISABLE_BACKGROUND`  | Skip watcher + workers (useful for tests)                     |

## Deploying on GCP

The supported production target is a single-instance, per-customer deployment in the customer's own GCP project. Everything is co-located: app, worker, embedded Qdrant, SQLite, and CAS share one persistent disk; uploads and Google Drive sync land in the same volume. There is no horizontal scaling — indexing is deliberately serial (see [config.py](./src/voitta_rag_enterprise/config.py)).

The runtime shape is described below; the Terraform module that provisions it is under [`terraform/`](./terraform/) — see [terraform/README.md](./terraform/README.md) for the per-customer setup steps. The container image is built by [`.github/workflows/image.yml`](./.github/workflows/image.yml) from [`Dockerfile`](./Dockerfile) and published to `ghcr.io/<owner>/voitta-rag-enterprise:<tag>`.

### Compute: pick the right CPU family

Embedding (e5-base, SigLIP-2, fastembed BM25) and PDF parsing (MinerU layout/OCR) are matmul-heavy. CPU choice changes throughput by 2–4× even before considering GPUs.

| Family    | Silicon                | AVX-512 | AMX¹ | Notes                                  |
|-----------|------------------------|:-------:|:----:|----------------------------------------|
| E2 / N1   | mixed / Skylake        | ⚠       | ✗    | Avoid — variable, no AMX               |
| N2        | Cascade / Ice Lake     | ✓       | ✗    | OK fallback                            |
| N2D / T2D | AMD Milan              | ✗       | ✗    | Avoid for ML inference                 |
| C2        | Cascade Lake           | ✓       | ✗    | OK                                     |
| **C3**    | **Sapphire Rapids**    | ✓       | **✓**| Recommended                            |
| **C4**    | **Emerald Rapids**     | ✓       | **✓**| Recommended (newer than C3)            |
| C3D       | AMD Genoa (Zen 4)      | ✓       | ✗    | Decent but no AMX                      |
| G2        | + NVIDIA L4 GPU        | —       | —    | Use when GPU acceleration is wanted    |

¹ AMX (Advanced Matrix Extensions) is the on-CPU tile matmul unit on Sapphire/Emerald Rapids. ONNX Runtime (fastembed) and PyTorch via oneDNN emit AMX automatically for INT8 / BF16 — this is what closes most of the gap to entry-level GPUs for embedding workloads.

**Default**: `c4-standard-8` (8 vCPU / 32 GB), or `c3-standard-8` if C4 isn't available in the region. Falls back to `n2-standard-8` cleanly if neither is offered.

**Optional GPU**: `g2-standard-8` (1× L4) lights up MinerU and the embedders for noticeably better ingest throughput. The app picks GPU automatically when CUDA is available; no config change required.

### Cluster: GKE Standard, single-node

GKE **Standard** (not Autopilot) with a single-node pool pinned to C4. Autopilot's compute classes can target C-family silicon but at a per-pod premium and with less deterministic scheduling — Standard is simpler and cheaper for a fixed single-replica workload.

- Deployment with `replicas: 1`, `strategy: Recreate` (avoids two writers on SQLite/Qdrant).
- 200 GB balanced PD mounted at `VOITTA_DATA_DIR`. `VOITTA_ROOT_PATH` is a subdirectory of the same volume — uploads + Google Drive mirrors live there.
- HTTPS Ingress with a reserved global static IP. The Terraform outputs the IP; the customer points an A record at it and provisions a managed cert (manual one-time step per customer).
- ConfigMap-mounted `users.txt` rendered from a Terraform `extra_users` variable; Secret Manager (via the CSI driver) for OAuth client id/secret and the session secret.
- Outbound egress for Google Drive sync and (during first boot) any model downloads not baked into the image.

### Auth: domain allowlist + extras

Each deploy has an OAuth client in the customer's GCP project. Allowed sign-ins are: every verified email at one of `VOITTA_ALLOWED_DOMAINS` (e.g. `customer.com`), plus any address listed in `users.txt` (consultants, contractors). Anyone else is rejected at the OAuth callback. With **both** lists empty every sign-in is denied — a deliberate fail-loud default.

### Container image

Built by GitHub Actions on tag and published to GHCR. Customers consume it by tag — they do not build anything.

- Base: `python:3.12-slim` + system deps for MinerU, cairo, poppler.
- Models baked into the image at build time: e5-base, SigLIP-2, fastembed BM25, and MinerU's pipeline weights. This makes the image ~6–8 GB but gives instant cold-start and works in egress-restricted environments.
- Pinned by version tag (`v0.x.y`); the customer's tfvars references a specific tag.

The local terminal flow (`make dev`) is unaffected by any of this — it doesn't use Docker, doesn't pull weights, and doesn't go through Google OAuth (it uses `VOITTA_DEV_USER`).

### Updating a deployment

Bump `image_uri` in the customer's tfvars and `terraform apply`. The single replica recreates with ~30–60s of downtime — there is no blue/green path because the embedded Qdrant + SQLite cannot be safely run from two pods at once.

## Status

All eight implementation stages are complete. 200+ tests; CI runs lint + tests on every PR.

## License

`Voitta RAG Enterprise` is dual-licensed:

- **[AGPL-3.0-or-later](./LICENSE)** for open-source use, self-hosting, and contributions.
- **Commercial license** for embedding in proprietary products or running modified hosted versions without AGPL §13 obligations — contact **support@voitta.ai**.

See [LICENSING.md](./LICENSING.md) for details. Contributors must sign the [CLA](./CONTRIBUTING.md#contributor-license-agreement).
