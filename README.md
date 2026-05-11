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

### Single-user mode (no auth, single-box deploy)

Skip Make and run uvicorn directly. Works on any host as long as `VOITTA_ROOT_PATH` exists and is writable:

```bash
python -m venv .venv && .venv/bin/pip install -e .
VOITTA_SINGLE_USER=true \
VOITTA_ROOT_PATH=/mnt/ssddata/data/voitta-rag-enterprise \
.venv/bin/uvicorn voitta_rag_enterprise.main:app --host 0.0.0.0 --port 10000
```

Everything (managed folders, ACL checks) collapses onto a built-in `root` user — no Google OAuth, no headers needed. Open `http://<host>:10000/`.

## Real models

Fake embedders are deterministic (hash-based) and good for development. For real RAG quality just unset the toggle — `pip install -e .` already pulls in everything the embedders need:

```bash
unset VOITTA_USE_FAKE_EMBEDDERS
make dev
```

The dense (`intfloat/e5-base-v2`), sparse (`Qdrant/bm25`), and image (`google/siglip2-base-patch16-224`) models download lazily on first use.

## MCP server

```bash
make mcp                    # listens on $VOITTA_MCP_PORT (8001)
```

Exposes 12 tools:

- **Search & retrieval** — `search`, `search_images`, `get_file`, `get_chunk_range`, `get_chunk_images`, `get_image`, `list_indexed_folders`, `resolve_url`.
- **Page-level views** — `list_page_images`, `get_page_image` (per-page WebP renders for PDFs + cross-file Workspace slide thumbnails).
- **On-demand assets** — `list_assets`, `request_asset` (mint signed URLs for CAD projections and other parser-declared derived views; URLs are absolute when `VOITTA_PUBLIC_BASE_URL` is set, otherwise relative paths).

ACL identity comes from the `X-User-Name` header.

## CAD support

STEP (`.step` / `.stp`) and FreeCAD native (`.FCStd`) files are indexed into a component tree and rendered on demand:

- **Parsing.** `cadquery-ocp` (PyPI wheel — ~500 MB, bundles OpenCASCADE 7.x) reads both formats via `STEPCAFControl_Reader` and `BRepTools.Read_s`. No FreeCAD install required; `.FCStd` is opened directly as a zip, `Document.xml` for the App::Part tree and per-feature `<name>.Shape.brp` blobs for geometry.
- **Tessellation.** `BRepMesh_IncrementalMesh` with linear 0.5 mm / angular 0.5 rad tolerances — same defaults as Creality Print, loose enough for big assemblies to finish quickly, tight enough that 320 px previews stay sharp.
- **Rendering.** Headless VTK (the PyPI wheel) writes PNGs through `vtkRenderWindow` with `SetOffScreenRendering(1)`. The wheel dlopens the host's OpenGL: EGL + Mesa Gallium llvmpipe in production, OSMesa fallback otherwise. With a GPU available (e.g. `g2-standard-8`) VTK picks the hardware path automatically.
- **Slugs.** Each App::Part becomes a renderable component; a synthetic `whole-assembly` slug rolls up everything when there's no single-root container.
- **Camera framing.** Robust 5–95 percentile bbox + view-aligned u/v extent projection so iso/front/top/side all fill the frame consistently. Outlier feature placements (broken FreeCAD Mirror history, "deleted by moving 150 m away") are dropped via a 1.5×IQR fence on translation magnitude before framing.
- **On-demand contract.** Render is never done at index time — `request_asset(file_id=N, asset_type="cad_projection", slug=...)` mints four signed URLs (front, top, side, iso) good for ~7 days. Per-render cost is dominated by tessellation + VTK; a 670-part assembly renders in ~1.5 s on CPU.

Code: [`services/parsers/cad_step_parser.py`](./src/voitta_rag_enterprise/services/parsers/cad_step_parser.py), [`services/parsers/cad_fcstd_parser.py`](./src/voitta_rag_enterprise/services/parsers/cad_fcstd_parser.py), [`services/cad_render.py`](./src/voitta_rag_enterprise/services/cad_render.py). Outside of `cadquery-ocp` and `vtk` (both in core deps), the CAD path needs no extra apt packages beyond the EGL/Mesa stack already in the Dockerfile.

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
| `VOITTA_PUBLIC_BASE_URL`     | Public origin for signed asset URLs (e.g. `https://rag.customer.com`). Set in prod so MCP clients receive absolute URLs; leave empty in local dev. |

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

### Runtime shape: one Compute Engine VM

A single VM running [Container-Optimized OS](https://cloud.google.com/container-optimized-os) with two Docker containers managed as systemd units:

- **`voitta`** — the app, listening on `127.0.0.1:8000`.
- **`caddy`** — reverse proxy + automatic Let's Encrypt TLS on `:443`, with `:80` serving only the ACME challenge and a 308 redirect to HTTPS.

Storage:

- A 200 GB `hyperdisk-balanced` PD attached at `/mnt/disks/voitta`, mounted persistently across VM lifetimes. Holds `VOITTA_DATA_DIR` (SQLite + CAS + embedded Qdrant) and `VOITTA_ROOT_PATH` (uploads + Drive mirror).
- The boot disk is ephemeral. VM replacement (e.g. on image upgrade) preserves the data PD; only the docker image cache is lost.

Why not GKE: it was the original target, but for a single-replica stateful workload the k8s control-plane fee, PVC abstraction, and Service+Ingress LB layer added cost and complexity for zero benefit. The GCE VM path is ~250 lines of HCL vs. ~600 for the GKE equivalent.

### TLS

Caddy fetches a Let's Encrypt cert via HTTP-01 on first request and renews automatically. Set `var.domain` to the FQDN; leave it empty during bring-up and Caddy serves plain HTTP on `:80` until DNS is wired. Switching from HTTP to HTTPS requires recreating the VM (cloud-init only runs on first boot) — `terraform apply -replace=...google_compute_instance.this` does this; the data PD persists.

### Authentication

**Users sign in with Google.** The app never stores or sees a password — it relies on Google's OAuth 2.0 flow, reads the verified email from Google's response, and decides admit-or-deny against a runtime allowlist managed by admins inside the app.

```
[user's browser]  ──▶  [Google consent screen]  ──▶  [/api/auth/google/callback]
                                                              │
                                                              ▼
                                          ┌──────────────────────────────┐
                                          │ verified email checked vs:   │
                                          │   • super_admins (env var)   │
                                          │   • allowed_domains.txt      │
                                          │   • allowed_users.txt        │
                                          │   • blocked_users.txt (deny) │
                                          └──────────────────────────────┘
```

**Two modes** — toggled by whether `VOITTA_GOOGLE_AUTH_CLIENT_ID/SECRET` are set:

| Mode | When to use | Sign-in screen |
|---|---|---|
| **OAuth on** (production) | Customer-facing deploys | "Sign in with Google" button |
| **OAuth off** (dev) | Local `make dev`, smoke tests | None — auto-signed-in as `VOITTA_DEV_USER` |

**The allowlist is in-app, not in env vars.** Admins manage three lists from the **🔒 Admin** panel:

- **Allowed domains** — anyone with a verified Google account in one of these domains can sign in.
- **Users** — individual addresses, with optional admin grant in the same step. Use this to admit / promote one person whose domain isn't on the list.
- **Blocked** — trumps everything (including super-admins). Use to revoke compromised accounts.

The three lists are persisted as plain text files on the data PD (`<data_dir>/admin/{allowed_domains,allowed_users,blocked_users}.txt`). Also human-editable via SSH for emergency lockout recovery.

**Bootstrap admin via Terraform**: `super_admins` (env: `VOITTA_SUPER_ADMINS`) is a list of email addresses that are *always* admitted at sign-in (block-list aside) and re-stamped `is_admin=True` on every login. This is how a fresh deploy gets its first admin, and the recovery path if every admin gets demoted in the DB. With this env var empty AND the in-app allowlist empty, every sign-in is denied — a deliberate fail-loud default.

**Impersonation**: admins can "View as" any user from the panel — useful for debugging "what does X see in their folder list?" Admin status is real-identity, never inherited via impersonation.

For per-customer deployment instructions including the manual GCP-console OAuth setup, see [terraform/README.md](./terraform/README.md).

### Container image

Built by GitHub Actions on tag and published to GHCR. Customers consume it by tag — they do not build anything.

- Base: `python:3.12-slim` + apt deps for MinerU (cairo, poppler, fonts) and headless CAD rendering (`libegl1`, `libgl1-mesa-dri`, `libosmesa6` — the EGL + Mesa Gallium llvmpipe path VTK uses when no GPU is attached).
- Python deps: `cadquery-ocp` for OpenCASCADE 7.x bindings (~500 MB wheel) and `vtk` for the offscreen renderer (~80 MB wheel) ship in core — no extras flag.
- Models baked into the image at build time: e5-base, SigLIP-2, fastembed BM25, and MinerU's pipeline weights. This makes the image ~6–8 GB but gives instant cold-start and works in egress-restricted environments.
- Pinned by version tag (`v0.x.y`); the customer's tfvars references a specific tag.

The local terminal flow (`make dev`) is unaffected by any of this — it doesn't use Docker, doesn't pull weights, and doesn't go through Google OAuth (it uses `VOITTA_DEV_USER`).

### Updating a deployment

Bump `image_uri` in the customer's tfvars and `terraform apply -replace=module.voitta_rag.google_compute_instance.this`. The VM is recreated and re-pulls the image; the data PD is a separate resource so app state survives. Downtime ≈ image-pull time on the new VM (a few minutes for the ~14 GB cold pull). There is no blue/green path because the embedded Qdrant + SQLite cannot be safely run from two VMs at once.

## Status

All eight implementation stages are complete. 200+ tests; CI runs lint + tests on every PR.

## License

`Voitta RAG Enterprise` is dual-licensed:

- **[AGPL-3.0-or-later](./LICENSE)** for open-source use, self-hosting, and contributions.
- **Commercial license** for embedding in proprietary products or running modified hosted versions without AGPL §13 obligations — contact **support@voitta.ai**.

See [LICENSING.md](./LICENSING.md) for details. Contributors must sign the [CLA](./CONTRIBUTING.md#contributor-license-agreement).
