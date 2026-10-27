# EmbeddingGemma-2 migration

Status: implemented on branch `gemma` (on top of `mineru4`), 2026-10-27. See "Implementation notes" at the end for what was decided while building and where it differs from the plan.

## Decision

Switch the default embedding stack from `e5-base-v2` (text) + `SigLIP-2` (images) to a single
`google/embeddinggemma-2` model for both text and images. Keep BM25 keyword fusion exactly as
deployed today (equal-weight RRF, top-20 from each list). Keep the e5 + SigLIP stack available as
a named profile so existing installs keep working and the desktop app keeps a fast CPU option.

Everything measured lives in `~/Downloads/embedding-model-evaluation-2026-10.docx` (report),
`~/emb-bench` on moon (public-benchmark harness), `~/emb-bench/oc` (own-corpus test) and
`~/emb-bench/img` (image test). Only numbers we measured ourselves are cited anywhere.

### What was measured

| Test | e5 / SigLIP as deployed | EmbeddingGemma-2 | Notes |
| --- | --- | --- | --- |
| Public text retrieval, 5 BEIR sets, mean nDCG@10, dense only | 42.2 | 46.1 | GTE-ModernBERT 52.0, Granite-English-R2 48.6 scored higher here |
| Own folders, 268 LLM-written queries, MRR@10, 1:1 fusion | 50.2 | +6.4 | tied with Qwen3-0.6B (+7.4) and Granite-English-R2 (+6.6) |
| Same, after a 48-query blinded false-negative audit | 71.4 | **+12.1 [+2.9, +21.7]** | the only model still significantly ahead |
| Images, 20,000 of our figures, 654 queries, MRR@10 | 0.3 (production bug) / 48.3 (SigLIP padded correctly) | **76.5** | +28.2 [+23.2, +33.0] over correctly padded SigLIP |
| Indexing speed on RTX 5070, 512-token chunks | 466 chunks/s | 128 chunks/s | about 2 h per million chunks |
| Image embedding speed on RTX 5070 | 222 img/s | 20 img/s | about 1 h for the 82k figures on saturn |
| Peak GPU memory, text+vision loaded | 1.5 GB (two models) | 2.2 GB | audio tower disabled |

Fusion finding that overrides the public-benchmark section of the report: on our own folders,
equal-weight 1:1 fusion was the best setting for every model on every slice (one exception:
Qwen3-0.6B on keyword queries, 3:1 marginally better). Dense-only was the worst setting. Do not
change fusion weights.

### Production bug found on the way

`services/embedding/image.py:136` encodes SigLIP text queries with `padding=True`. SigLIP-2 was
trained with `padding="max_length", max_length=64`; without it, live image search on saturn
returns near-random results (flat ~0.05 scores, confirmed with three queries). The fix is
query-side only and needs no re-embedding. It ships first, on its own, so existing installs are
repaired regardless of when they move to Gemma.

## Model facts that shape the design

- `google/embeddinggemma-2` is Apache-2.0, **not gated**, downloads anonymously (verified
  against the Hub API 2026-10-27). 1.5 GB on disk vs 2.1 GB (e5) + 1.5 GB (SigLIP).
- 768-dimensional output for text, images, video and audio in one space. Matryoshka-trained
  (256-d truncation measured in the report).
- Requires `transformers>=5.18` (`config.json` `transformers_version`) and loads through
  `sentence-transformers` (benchmarked on 6.1 / transformers 5.20 / torch 2.14).
- Text documents use `title: {title} | text: {text}`; queries use the `SearchQuery` prompt
  (`task: search result | query: `). Images go in as raw pixels with no text prefix. We measured
  images at the default 280 vision tokens; a 560-token pass was queued on moon
  (`~/emb-bench/img/emb/gemma560.npy`) and only informs whether more detail helps.
- Load with `config_kwargs={"audio_config": None}` to skip the audio tower. For text-only
  contexts (query embedding on a CPU box) `vision_config: None` also works.
- All three models (e5, SigLIP-2, Gemma) emit 768-d vectors. Qdrant will silently accept
  vectors from the wrong model into an existing collection. This is the main migration hazard.

## The transformers conflict

Our PDF parser depends on `mineru[pipeline]` (MinerU 3.x), and every MinerU 3 release pins
`transformers<5`. EmbeddingGemma-2 needs `transformers>=5.18`. Saturn runs MinerU 3.1.7 /
transformers 4.57.6; the desktop lock pins 3.2.3 / 4.57.6.

Resolution, in order of preference:

1. **MinerU 4** (`mineru[torch]>=4.0.12,<5` requires `transformers>=5.10.1,<6`). This is the
   proper fix and is planned separately in [mineru4-conversion.md](mineru4-conversion.md). The
   Gemma branch should be based on the MinerU 4 branch once that passes its quality gate.
2. **Fallback if MinerU 4 fails its quality gate:** keep MinerU 3 but give the parser subprocess
   its own virtualenv with transformers 4. `_MineruDaemon` already runs MinerU out of process
   over stdio, so the change is pointing it at a second interpreter. The desktop installer would
   create that second environment at first launch. More moving parts, but always works.
3. Not acceptable: overriding MinerU 3's pin and hoping. No measured evidence it works.

## Design

### Profiles

New setting `embedding_profile` in `config.py`, env `VOITTA_EMBEDDING_PROFILE`:

| Profile | Text | Images | Sparse | Default for |
| --- | --- | --- | --- | --- |
| `gemma2` | EmbeddingGemma-2 | EmbeddingGemma-2 (same instance) | BM25 (fastembed) | server, Docker |
| `e5-siglip` | e5-base-v2 | SigLIP-2 base | BM25 (fastembed) | desktop app |

The profile sets `dense_model`, `image_model`, `dense_version`, `image_version` and the
collection names (below). Each may still be overridden individually. The desktop launcher sets
`VOITTA_EMBEDDING_PROFILE=e5-siglip` beside its other defaults in
`src/voitta_rag_desktop/__main__.py` (around line 200). Server default is `gemma2`.

Resolution order for the active profile, highest first:

1. `VOITTA_EMBEDDING_PROFILE` set explicitly in the environment.
2. The `embedding_profile` row in the SQLite `settings` table — written by the migration
   command when it completes, so a migrated install stays on its new profile across restarts
   without anyone editing `.env`.
3. The install-type default (`gemma2` server, `e5-siglip` desktop).

Rule for existing installs: at startup the app compares the resolved profile against the
`collection_meta` table (below). If the resolved profile came from a default (step 3) and the
only populated collections belong to `e5-siglip`, the app adopts `e5-siglip`, logs that it did,
and shows "running the legacy profile; run the migration to move to gemma2" in the settings
overview. If the profile was set explicitly (step 1 or 2) and the populated collections belong
to a different profile, the app refuses to start indexing or search and prints the two ways out
(set the matching profile, or run the migration). It never writes vectors from one model into
another model's collection. New installs start on `gemma2` with nothing to reconcile.

### One model, one embedder

- New `services/embedding/gemma.py`: `GemmaEmbedder` implementing both `TextEmbedder` and
  `ImageEmbedder` from `services/embedding/types.py`. Public interfaces do not change.
  - `embed_passages(texts, titles)`: `title: {title or 'none'} | text: {text}`; 512-token cap
    as today; bf16 on CUDA, fp32 on CPU/MPS.
  - `embed_query(text)`: `SearchQuery` prompt.
  - `embed_images(pils)`: raw images, 280 vision tokens, batch 16 on a 12 GB card.
  - Loaded once under `gpu_lock`, audio tower disabled.
- `services/embedding/factory.py` returns the same `GemmaEmbedder` instance for
  `get_text_embedder()` and `get_image_embedder()` under `gemma2`; separate instances under
  `e5-siglip`.
- Query path: `api/routes/search.py`, MCP `search`/`search_images` and the assistant embed the
  query once per request and reuse the vector for both the chunk search and the image search.
  Under `e5-siglip` this is two embeds as today; the call site is the same.
- Images keep their own collection and their own result list. Text-to-image cosines are not on
  the same scale as text-to-text; a merged ranking would bury images. Not changing this.

### Title slot

Our own-corpus test passed `title: none`. The file's `rel_path` is available at embed time and
costs nothing to pass. Before `GemmaEmbedder` is finalised, re-run the 268-query test on moon
with `title: {rel_path}` vs `title: none` (about an hour, harness in `~/emb-bench/oc`,
`oc_embed.py` takes the title format). Ship whichever scores higher; record the result here.

### Collections per profile

- Chunk and image collections are named per profile: `chunks__gemma2`, `images__gemma2`.
  Existing installs keep their current unsuffixed names for `e5-siglip`, so nothing moves.
- `vector_store.py` resolves names from the profile. A new SQLite table `collection_meta`
  (`collection_name`, `profile`, `dense_model`, `image_model`, `dim`, `created_at`) records what
  each collection was built with; `ensure_collections()` writes a row when it creates a
  collection and, for pre-existing unsuffixed collections with no row, backfills one as
  `e5-siglip`. Startup check: every collection the resolved profile uses must have a row with
  the same profile and the vector size Qdrant reports must be 768; otherwise the mismatch rule
  above applies. (Qdrant has no per-collection metadata of its own, which is why this lives in
  SQLite rather than on a sentinel point.)
- Orphan sweeps and other maintenance only ever touch the active profile's collections.

### Migration command

`scripts/migrate_embedding_profile.py --to gemma2`:

1. Creates the target collections.
2. Re-embeds every chunk from the text already stored in SQL (`chunks.text`), with titles, into
   the target chunk collection; re-embeds every image blob from the CAS into the target image
   collection. No PDF re-parsing.
3. Stamps `dense_version` / `image_version` per file as it goes, so an interrupted run resumes.
4. On completion writes `embedding_profile = gemma2` to the `settings` table (resolution step 2
   above) and prints a reminder if `VOITTA_EMBEDDING_PROFILE` is also set in the environment,
   since the env value would win. Old collections are left in place for rollback and can be
   dropped with `--drop-old` later.
5. Runs while the app is up: it reads SQL and the CAS, writes only to the new collections, and
   takes `gpu_lock` per batch like the indexer does. Rollback at any point is "do nothing";
   the old profile keeps serving.

`scripts/reembed_stale.py` is extended to understand profiles rather than duplicated.

### Text that names the models

MCP server instructions (`mcp_server.py` ~85–113), the `gpu_lock` docstring, the settings
overview, the Docker prewarm script and the README name e5/SigLIP explicitly today. Each reads
the active profile instead.

### Dependencies

- `transformers>=5.18,<6`, `sentence-transformers>=6.1`, `torch>=2.14` (the benchmarked set).
- MinerU per the conflict section above.
- Docker warm stage downloads the active profile's models; `gemma2` makes the image ~2 GB
  smaller than today's e5+SigLIP.

## Work plan

Branch `gemma`, based on saturn's commit `6ede80b` (a MinerU fix that was never pushed; fetch it
over ssh, read-only). Once `mineru4` lands, rebase `gemma` onto it.

1. **PR: SigLIP query padding fix** — one line in `services/embedding/image.py` plus a unit test
   asserting the processor is called with `padding="max_length", max_length=64, truncation=True`.
   Ships first and independently. No re-embed needed.
2. **Title-slot test on moon** (no code in the repo) — decides the passage format.
3. **PR: embedding profiles + GemmaEmbedder** — config, factory, embedder, single query embed,
   collection naming, startup check, settings overview, docs. Unit tests: profile resolution,
   shared instance under `gemma2`, separate under `e5-siglip`, collection names, startup check
   fires on a mismatched collection, query embedded once per request (spy on the embedder).
   Integration: existing fake-embedder suite under both profiles. One slow, opt-in test that
   loads the real model and checks a text and an image embedding have norm 1 and dim 768.
4. **PR: migration command** — `migrate_embedding_profile.py`, resumable, with a dry run that
   reports counts and estimated time from the measured throughputs.
5. **PR: docs** — README profile section, OPERATIONS migration runbook, MCP instruction text.

## Test deployment on moon (not saturn)

Moon (`192.168.88.210`, RTX 5070 12 GB, shared with nori-demos ~1.5 GB) runs a blank install
of the `gemma` branch. Saturn is not touched except for one nginx site.

- Checkout `~/DEVEL/voitta-rag-gemma`, own venv (the `~/emb-bench` venv stays separate).
- Qdrant in a new container `qdrant-gemma`, image pinned to `qdrant/qdrant:v1.16.3` (saturn
  runs 1.16.3; moon's existing `qdrant/qdrant:latest` happens to be 1.16.3 today but is not
  pinned, so the test container names the tag explicitly).
- App in `screen` session `voitta-gemma` on port 10000 (8000 is taken on moon), started the
  same way saturn starts it, plus `--forwarded-allow-ips 192.168.88.212`.
- `.env`: saturn's Google OAuth client id/secret, `PUBLIC_BASE_URL=https://gemma.voitta.ai`,
  `VOITTA_EMBEDDING_PROFILE=gemma2`, super admin = Roman, MinerU GPU budget ~5 GB
  (nori-demos 1.5 + Gemma 2.2 + MinerU fits in 12 GB).
- Fresh database, empty collections. Folders and users are added through the UI.

### Networking

Public 443 is forwarded to saturn (`71.105.133.69` → `192.168.88.212`), whose nginx terminates
TLS. `moon.neurc.com` → `voitta.duckdns.org` → that IP. Port 10000 on moon answers "connection
refused" from saturn today (reachable, nothing listening), so no firewall change is needed.

1. Route 53, zone `voitta.ai` (`Z08985463NJP6CWZCJIN5`): CNAME `gemma.voitta.ai` →
   `moon.neurc.com`, TTL 300. Done with `aws route53 change-resource-record-sets`.
2. nginx on saturn (needs sudo, run by Roman). Two files prepared in `~`:
   a port-80-only site with the ACME challenge location, and the full site (a copy of
   `enterprise.voitta.ai` pointed at `http://192.168.88.210:10000`). Order matters: enable the
   port-80 file, `nginx -t`, reload, `certbot certonly --webroot -w /var/www/html -d
   gemma.voitta.ai`, then replace with the full file, `nginx -t`, reload.
3. Google OAuth client `300344106100-…`: add redirect URI
   `https://gemma.voitta.ai/api/auth/google/callback` (path confirmed in
   `api/routes/auth.py::_redirect_uri`, router prefix `/auth` under `/api`). gcloud cannot edit a
   standard OAuth client; this is one action in Cloud Console. The Microsoft login for the Sensium tenant is
   not configured for gemma.voitta.ai; Google login only on the test host.

### Acceptance on moon

- Login works for Roman and one other user; each sees only their folders.
- Add a code folder, a papers folder and a business folder; text and image search return
  sensible results.
- Run the 268 own-folder queries (`~/emb-bench/oc/queries.json`) against the live `/search`
  endpoint; MRR@10 should match the benchmark for Gemma at 1:1 fusion within noise.
- Upload a PDF; it parses and indexes (exercises MinerU under the new transformers).
- `nvidia-smi` steady state under 12 GB with nori-demos still running.

### Acceptance results (2026-10-27)

Scale migration on moon: an eval install built from the benchmark corpus (13 folders, 5,793
text files, 141,598 chunks, 20,000 figures; SQL + CAS only), first embedded as `e5-siglip`,
then moved with `migrate_embedding_profile.py --to gemma2` (bulk with the app able to run,
then `--finish`). Scored through the live `POST /api/search` (top-20 dense + top-20 BM25, RRF,
as deployed); paired bootstrap 95 % CI.

| | e5-siglip | gemma2 | delta |
| --- | --- | --- | --- |
| Text, 268 own-folder queries, MRR@10 | 51.8 | 58.3 | **+6.5 [+2.0, +11.2]** (101 better, 55 worse) |
| Images, 654 queries, MRR@10 | 48.3 | 82.6 | **+34.3 [+30.8, +37.9]** (317 better, 45 worse) |

- e5 images 48.3 equals the benchmark's correctly padded SigLIP: the padding fix works live.
- Gemma images 82.6 vs benchmark 82.3 (560 vision tokens); text 58.3 vs ~59.6 expected from
  the benchmark (+6.4 model, +3.0 rel_path title) — within noise.
- Migration time on the RTX 5070: text 22 min (141,598 chunks, ~109/s), figures 41 min
  (20,000, ~8/s); `--finish` 5 s. The e5 index of the same data took 47 min.
- No failed items. Steady-state VRAM with nori-demos, the gemma.voitta.ai app and the eval
  app all loaded: 3.7 GB of 12 GB.
- The startup guard fired as designed when the eval app inherited the gemma deployment's
  `.env` (`VOITTA_EMBEDDING_PROFILE=gemma2` against data recorded as `e5-siglip`).

### Rollback

Stop the screen session and the `qdrant-gemma` container; delete the DNS record and the nginx
site; remove `~/DEVEL/voitta-rag-gemma` and the Qdrant data dir. Saturn is unchanged throughout.

## Saturn, later

Saturn lost its chunk vectors on 2026-10-02 (`~/Downloads/voitta-qdrant-vector-loss-2026-10.md`)
and must re-embed 34 folders anyway. When Roman decides to deploy, restore straight into the
`gemma2` collections via the migration command. Expect roughly 2 h per million chunks and 1 h for
the 82k figures on the 3090, likely somewhat faster than the measured RTX 5070 numbers.

## Open items

- [x] Title-slot result: `title: {rel_path}` wins, see below. Shipped.
- [x] 560 vision tokens beat 280, see below. Shipped as the default.
- [x] MinerU 4 passed its gate ([mineru4-conversion.md](mineru4-conversion.md)): resolution 1.
- [ ] Desktop: confirm e5-siglip remains the default there, and that `gemma2` is selectable for
      users with Apple Silicon (speed unmeasured on MPS; measure before advertising).

## Implementation notes (2026-10-27)

### Measured while building

- **Title slot.** Same 268 own-folder queries and harness as the evaluation. MRR@10 at the
  deployed 1:1 fusion: `title: none` 56.6, `title: {rel_path}` 59.5, **+3.0 [+0.6, +5.3]**
  (56 queries better, 32 worse). Dense alone +7.1 [+3.6, +10.6]. By domain: code +10.0
  [+5.9, +14.4], business −2.3 [−6.0, +1.3], papers −0.2 [−4.0, +3.4]. Shipped `rel_path`.
  (The absolute numbers differ from the table above, which used the audited labels; the
  comparison is paired on identical labels.)
- **Vision tokens.** 20,000 figures, 654 queries: 560 tokens 82.3 MRR@10 vs 280 tokens 76.5,
  **+5.8 [+3.8, +8.0]** (100 better, 40 worse), at 2.4x the time (2,461 s vs 1,011 s for
  20k images, 8 vs 20 img/s) and 2.26 vs 2.16 GB peak. Shipped 560, batch 8.
- **Parity.** The app's `GemmaEmbedder` reproduces the benchmark vectors: passages with
  titles, queries, image queries and images at 560 tokens all at cosine ≥ 0.9999 against the
  stored evaluation vectors; batched and single-image encodes agree; the fixed SigLIP query
  matches the padded harness at 1.0000. transformers 5.21 / sentence-transformers 6.2.
- **Bug found by the parity run:** sentence-transformers keeps one `max_seq_length` per
  model, so the 512-token passage cap truncated image inputs (560 vision tokens + template)
  and the processor raised. The cap is now set per call under `gpu_lock`; an opt-in test
  (`tests/integration/test_real_embedders.py`) guards it.

### Differences from the plan

- The persisted profile lives in a new `app_settings` table (key `embedding_profile`), not a
  `settings` table (none existed).
- A mismatch **aborts startup** instead of starting with indexing and search disabled: the
  message names both ways out, and nothing can write to the wrong space.
- A migration marks itself in progress (`app_settings.embedding_migration`); while set, an
  app restart never adopts the half-filled profile. Not in the plan; needed because the
  default-profile rule would otherwise pick it up.
- The migration reuses the indexer's own code: `embed_text`/`embed_image` were split into a
  job wrapper and a shared core (`embed_file_chunks`, `embed_image_targets`), so payloads are
  built in one place. It diffs SQL against the target each pass (chunk ids, image file
  attachments, model versions) and repeats until a pass finds nothing to do.
- "Embed the query once per request" applies to `POST /api/search` (chunks + images in one
  request). MCP `search` / `search_images` and the assistant's tools are separate calls.
- MCP instructions are profile-neutral (the server is built at import, before a profile is
  resolved).
- Docker bakes in the profiles listed in the `PREWARM_PROFILES` build arg (default `gemma2`).
- The desktop launcher sets `VOITTA_DEFAULT_EMBEDDING_PROFILE=e5-siglip` (a default, so a
  desktop user who migrates stays migrated), not `VOITTA_EMBEDDING_PROFILE`.
- Fixed on the way: `/api/search` returned 500 when a collection did not exist yet (fresh
  install, or images mode before the first image); it now returns no hits.
- Fixed on the way: `test_tampered_signature_rejected` was flaky — it flipped the last
  base64url character of an HMAC, whose padding bits decode away.
