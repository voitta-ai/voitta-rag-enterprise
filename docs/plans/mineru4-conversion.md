# MinerU 4 conversion

Status: plan, 2026-10-27. Not started. Prerequisite for the default `gemma2` embedding profile
([gemma-migration.md](gemma-migration.md)) because MinerU 3 pins `transformers<5` and
EmbeddingGemma-2 needs `transformers>=5.18`.

Facts below about MinerU 4 were read from the 4.0.12 release (2026-10-11), its PyPI metadata and
its `master` source and docs on 2026-10-27. Items marked **verify** were not confirmed against a
running install and must be checked in step 1 before any code is written against them.

## Why convert

- MinerU 4 (`mineru[torch]>=4.0,<5`) requires `transformers>=5.10.1,<6` and `torch>=2.7,<3`,
  which is compatible with EmbeddingGemma-2. Every MinerU 3 release (last: 3.4.5) requires
  `transformers<5`.
- Its `basic` tier is the successor of the 3.x `pipeline` backend: layout, OCR (PP-OCRv6),
  formula and table models, no VLM, ~0.8 GB of weights. It runs on CUDA/MPS through Torch or on
  CPU through ONNX, selected automatically.
- The `flash` tier (native text layer, no models) is a new fast path for text PDFs that the
  desktop app could use on weak machines. Not part of the acceptance gate; noted as an option.
- The `standard`/`advanced` tiers need a VLM (llama.cpp GGUF or vLLM, 8 GB+ VRAM) and are out
  of scope. The configuration must make them selectable later without code changes.

## What MinerU 4 changes that we touch

| Area | MinerU 3.x (what we use) | MinerU 4 | Our impact |
| --- | --- | --- | --- |
| Entry point | `mineru.cli.common.do_parse(backend="pipeline", parse_method, p_lang_list, f_dump_*)` writing files | `mineru.parser.MinerUParser(tier, parse_mode, image_analysis).parse(path, page_range)` → `ParseResult` | rewrite the subprocess body |
| Outputs | `<name>.md`, `<name>_content_list.json`, `images/` under `out/<name>/<method>/` | `result.save(FileBasedDataWriter(dir))` → `markdown.md`, `middle_json.json`, `structured_content.json`, `images/`. No content list is saved; produce it with `mineru.render.render_content_list(ParseResult.from_json(saved middle_json.json).middle_json)`. **Do not render from the unsaved `result.middle_json`**: before `save()` the image blocks hold `image_base64`, and `resolve_image_source` returns the data URI as `img_path` (verified in `docvortex/render/_internal/markdown/assets.py`). | rewrite output reading; drop path guessing |
| Image refs | `images/<hash>.jpg` | `images/page_{page_idx}_{parent_type}_{parent_index}.{ext}`, `_duplicate_n` suffix on collisions | `_resolve_image_path` simplifies |
| Content list fields | `type`, `page_idx`, `bbox` in PDF points, `text`, `text_level`, `img_path`, `img_caption`, `table_body` | same keys plus `image_caption`/`image_footnote`, `table_caption`/`table_footnote`, new types `chart`, `code` (`sub_type`), `list` (`list_items`), `index`, `page_footnote`; **`bbox` is 0–1000 per-mille of the page, ints**. Titles are `type: "text"` with `text_level` set (there is no `title` type; this was also true in 3.x, so our `_TITLE_TYPES = ("title",)` has never matched) | layout code, docs, fixtures |
| Page numbers | `page_idx` local to the parsed file (we split buckets and offset) | `page_idx` refers to the **original document** when `page_range` is used | bucket offset math goes away |
| OCR language | `p_lang_list=[lang]` | no language parameter; 3.4 routed en/latin to the `ch` model | `pdf_lang` setting is dead |
| Parse method | `parse_method="auto|txt|ocr"` | `parse_mode="auto|txt|ocr"` | same values, renamed |
| Config | `~/mineru.json` | `$MINERU_HOME/config.yaml` (`MINERU_HOME` default `~/.mineru`), `MINERU_CONFIG`; env overrides `MINERU_<FIELD_PATH>` (`MINERU_MODEL_SOURCE`, `MINERU_MODEL_SMALL_BACKEND`, …); `mineru.json` is **not read** | Dockerfile, desktop |
| Models | HF cache (`HF_HOME`), `mineru-models-download -m pipeline` | `model.base_dir` default `~/.mineru/models`; `mineru-kit models download --tier basic --small-backend torch|onnx --source huggingface`, `mineru-kit models verify`, `mineru-kit models show`; `mineru-models-download` console script still exists | prewarm script, Dockerfile, desktop installer |
| Page window | `MINERU_PROCESSING_WINDOW_SIZE` (3.x), `MINERU_MIN_BATCH_INFERENCE_SIZE` (2.x) | `MINERU_PROCESSING_WINDOW_SIZE` still read (default 64); the 2.x name is gone; `MINERU_MALLOC_TRIM` added | desktop launcher env |
| Model lifetime | loaded on first `do_parse`, cached in process | `HybridLocalModelContextSingleton` — process-level singleton; per-document `acquire/release` with memory cleanup | warm daemon still worthwhile |
| Page rendering | `mineru.utils.pdf_image_tools._get_pdf_render_executor` (we monkey-patch it) | rendering lives in `docvortex`, which spawns worker processes (`multiprocessing` `spawn` context) and exposes supported knobs: `DOCVORTEX_PDF_RENDER_BACKEND=auto|legacy|session` (auto → session), `DOCVORTEX_PDF_RENDER_THREADS` (process count, default 3; one process per 30 pages, max 3), `DOCVORTEX_PDF_RENDER_TIMEOUT` (default 300 s) | the patch goes; set the env knobs instead, see below |
| Errors | free-text; we regex PDFium "incorrect password" | `mineru.errors.MineruError(code, message)` with `InvalidRequestError`, `EngineError` (`parse_failed`, `parse_timeout`), `NotFoundError`. The code table includes `file_encrypted`, but the raise site for a password-protected PDF is not in readable source (docvortex's PDF core is a compiled module) — **verify** in step 1 what `MinerUParser.parse()` actually raises | map by class and code where typed; keep the message regex as a secondary classifier if step 1 shows a raw pdfium error |
| Logging | loguru | loguru (custom host sinks are preserved by `configure_global_log_level()`, which runs on every `parse()` and sets `LOGURU_LEVEL` from `config.log.level`, env `MINERU_LOG_LEVEL`) plus stdlib `logging` under `mineru.*` | keep `_redirect_loguru`; set `MINERU_LOG_LEVEL=WARNING` in the subprocess env; test the sink survives |
| Dependencies | `mineru[pipeline]`; `pdftext` forces `pypdfium2==4.30.0` | base package pulls `docvortex`, `pypdfium2>=5.10.1,<6`, `onnxruntime`, `gradio>=6.8`, `fastapi`, `uvicorn`, `pydantic>=2.12.5`, `loguru`, `typer`; `[torch]` adds torch/torchvision/transformers/accelerate/safetensors | constraints, lock file |

Behaviour we rely on that is **not** changing: PyMuPDF for page counting and page renders (ours,
not MinerU's); `gpu_lock` serialisation; the stdio daemon with SIGKILL watchdog; the
`ParserResult` contract to the rest of the app (`content`, `images`, `page_images`,
`page_layout`, `char_to_page`, `metadata`).

## Every piece we touch

### Code

| File | Change |
| --- | --- |
| `services/parsers/_mineru_subprocess.py` | Replace `do_parse` with `MinerUParser(tier, parse_mode, image_analysis=False).parse(path, page_range=...)` (`basic` maps to effort `medium`, which does not support image analysis per the 3.3 changelog; pass `False` explicitly rather than rely on it being ignored). Write outputs with `result.save(FileBasedDataWriter(out_dir))`, then reload `middle_json.json` through `ParseResult.from_json()` and write `render_content_list(...)` to `content_list.json` so `img_path` values are the materialised `images/...` paths. Translate `MineruError` into the JSON error response with its `code`. **Delete `_force_inline_pdf_rendering`** (private-API monkey-patch) — see "Render workers". Keep one `MinerUParser` per `(tier, parse_mode)` for the daemon's lifetime (`DocumentParser` has `close()`/context manager; call `close()` on shutdown). |
| `services/parsers/pdf_parser.py` | New module boundary: move all MinerU-output interpretation into `services/parsers/mineru_result.py` (pure functions over the saved files). `_parse_bucket` sends `page_range` instead of a pre-split bucket file; `_split_pdf` is deleted; `_merge_buckets` loses page offsetting (page_idx is absolute) but keeps markdown concatenation and char-offset cursoring; `_read_content_list` reads the one file we wrote; `_resolve_image_path` resolves `img_path` relative to the save dir only. The daemon raises `UnsupportedDocumentError` when the subprocess reports `code == "file_encrypted"`; `_ENCRYPTED_PDF_RE` is removed only if step 1 confirms the typed error, otherwise it stays as the classifier for the raw pdfium message. `_MineruDaemon.parse()` request schema becomes `{pdf_path, page_range, out_dir, tier, parse_mode}`. |
| `services/parsers/mineru_result.py` (new) | `load_saved_result(dir) -> SavedResult(markdown, content_list, images_dir)`; `image_page_map`, `layout_blocks`, `char_to_page_anchors` ported from today's `_image_page_map`, `_layout_for_bucket`, `_bucket_char_to_page` with: absolute `page = page_idx + 1`; anchor search over `text` (titles included, they are `text` with `text_level`), `list` (`list_items` joined), `code`, `page_footnote` blocks; `chart` treated like `image` for layout; `bbox` passed through as-is (per-mille). Fully unit-tested against a recorded fixture. |
| `services/layout.py` | Classify a title as a `text` block with an int `text_level` (replaces the never-matching `_TITLE_TYPES = ("title",)`; `n_title`, `max_text_level` and the `cover`/`quote` rules start working for real, so expect `layout_kind` distributions to shift — record before/after counts in the quality gate). Add `chart` to `_IMAGE_TYPES`, `code` and `list` to text-like counting, `page_footnote` to `_PAGE_NUMBER_TYPES`. Area/column heuristics are ratio-based (`_bbox_area_and_x0`, `_column_count`, `_layout_kind`) so the 0–1000 scale needs no numeric change; update the module docstring ("PDF points" → "per-mille of the page"). |
| `mcp_server.py` (`get_page_layout` docstring ~385–400), `services/parsers/base.py` (`ParserResult.page_layout` comment) | Same wording fix; mention the new block types. |
| `api/routes/files.py` `get_file_layout` | No change needed (uses `page`, `type`, `text`); add `chart`/`code` to the SPA's type icons if it has a fixed set (**verify** in `static/`). |
| `config.py` | Add `pdf_parse_tier: Literal["flash","basic","standard","advanced"] = "basic"`; rename `pdf_parse_method` → keep name, document that it maps to MinerU `parse_mode`; **remove `pdf_lang`** (dead) with a startup warning if the env var is still set. Reject `standard`/`advanced` at startup unless `mineru-kit models verify` for that tier passes (so the option exists but cannot half-work). |
| `services/settings_overview.py` | Replace `pdf_lang` with `pdf_parse_tier`. |
| `api/routes/jobs.py` (`cancel_all`, cancel-one) | No protocol change: they SIGKILL `pdf_parser._DAEMON._proc`. Update the two comments that say "MinerU subprocess" if wording changes; verify the kill still unblocks `readline()` (it is our pipe, so yes). |
| `logging_config.py` | `_redirect_loguru` stays. Add a regression test: after a parse, our `mineru.log` sink is still the only active sink. Quiet the stdlib `mineru` logger as today. |
| `scripts/prewarm_models.py` | Replace `mineru-models-download -s huggingface -m pipeline` with `mineru-kit models download --tier basic --small-backend torch --source huggingface` followed by `mineru-kit models verify --tier basic --small-backend torch`. Model source already comes from `MINERU_MODEL_SOURCE`. |
| `Dockerfile` | Set `MINERU_HOME=/opt/mineru` in the base `ENV` so the warm stage and the runtime share one model root; copy `/opt/mineru` from `warm`, `chown` it to `voitta`; **delete** the `mineru.json` copy/move lines (112–128). Keep the apt libs (`libgl1`, `libglib2.0-0`, …) — OpenCV is still in the small-model path (**verify** the exact list by building). |
| `constraints.txt` | Remove `pypdfium2==4.30.0` (pdftext is gone; MinerU 4 wants 5.x). Re-evaluate `typer<0.26` and `setuptools<82` against the new graph; keep only what a clean `pip install -c constraints.txt -e ".[ml]"` still needs. |
| `pyproject.toml` | `mineru[pipeline]>=2.0` → `mineru[torch]>=4.0.12,<5`; `transformers>=5.18,<6`; `sentence-transformers>=6.1`; comment block about `[pipeline]`/vllm rewritten. Note `gradio>=6.8` arrives as a hard dependency of the base package. |
| `src/voitta_rag_desktop/__main__.py` | Keep `MINERU_PROCESSING_WINDOW_SIZE=1` on darwin (still honoured, same reason: page-at-a-time keeps MPS memory flat); delete `MINERU_MIN_BATCH_INFERENCE_SIZE`. Comments about "MinerU's render pool uses spawn" stay true (docvortex workers). |
| `src/voitta_rag_desktop/installer.py` `_prewarm_models` | Add a MinerU step: `python -m mineru.kit.main models download --tier basic --source huggingface` via `sys.executable` (console scripts do not exist in the frozen app; **verify** `mineru.kit.main` is runnable with `-m`, else call `mineru.kit.commands.models.download_main()` in-process with `sys.argv` set). Best-effort like the embedder warm-up. |
| `src/voitta_rag_desktop/resources/requirements-lock.txt` | Regenerate from the new `pyproject.toml` on macOS arm64 (`mineru[torch]` is implied on Apple Silicon). Expect `mineru==4.0.12`, `docvortex`, `pypdfium2 5.x`, `transformers 5.x`, `torch 2.14`, removal of `pdftext`, `mineru-vl-utils` bumped to 2.x. |
| `desktop/build_app.sh`, `desktop/pyproject.toml` | Comments reference mineru/pylatexenc; check `pylatexenc` is still pulled (it is a MinerU 3 transitive) and drop the workaround comment if not. |

### Tests

| Test | Change |
| --- | --- |
| `tests/unit/test_mineru_result.py` (new) | Golden-fixture tests for `mineru_result.py`: a real MinerU 4 `basic` run on a 3-page PDF with one figure, one table and one two-column page, recorded once on moon and committed under `tests/fixtures/mineru4/` (`markdown.md`, `content_list.json`, `images/`). Asserts image→page mapping, layout blocks with `page`, char→page anchors, new block types. |
| `tests/unit/test_layout.py` | The `_txt(level=...)` fixture builds `type: "title"`, which MinerU never emits; change it to `type: "text"` + `text_level` and re-derive the expected `layout_kind` values. Fixtures switch to 0–1000 bboxes; add `chart`, `code`, `list` cases. |
| `tests/integration/test_mineru_daemon.py` | Stub protocol updated to the new request keys; add a case where the stub returns `{"status":"error","code":"file_encrypted"}` and the parser raises `UnsupportedDocumentError`. |
| `tests/unit/test_indexing_caps.py`, `tests/integration/test_admin_endpoints.py` | `pdf_lang` removed from settings assertions if present. |
| `tests/integration/test_real_mineru.py` (new, opt-in `@pytest.mark.real_mineru`) | Parses the fixture PDF through the real daemon; checks page count, ≥1 image, markdown non-empty, loguru sink intact. Run manually on moon and on the Mac, not in CI. |

### Docs

`README.md` (system deps note ~30–50, first-run notes ~326–332, Docker section ~420–428),
`docs/OPERATIONS.md` ("PDF parsing: the MinerU daemon" ~229–262, parser output matrix,
`gpu_lock` table ~1666, logging table ~1735–1737): tier wording, model download command, config
location, `pdf_parse_tier`, removal of `pdf_lang`, bbox units.

## Render workers (the monkey-patch)

Today `_mineru_subprocess._force_inline_pdf_rendering` replaces MinerU's private render executor
with an in-thread stub, for two reasons recorded in the code: the daemon parses one PDF at a
time so a pool buys nothing, and the frozen desktop app had trouble with spawn-launched workers
(now handled by the `-c` interpreter shim in `__main__.py`).

In MinerU 4 rendering is inside `docvortex` (`document/pdf/images.py`, `render_session.py`,
read from the 0.5.16 wheel) and the private symbol will not exist. Policy: **no private-API
patching**. docvortex has supported environment knobs and we use those:

- `DOCVORTEX_PDF_RENDER_THREADS=1` — one render process (the name says threads; the code
  documents it as a process count). docvortex already uses one process per 30 pages, so our
  20-page buckets get one process even at the default.
- `DOCVORTEX_PDF_RENDER_BACKEND=session` (the default resolution of `auto`); `legacy` exists as
  a fallback if the session worker misbehaves on a platform.
- `DOCVORTEX_PDF_RENDER_TIMEOUT` (default 300 s) must stay below our `pdf_parse_timeout_s`
  (600 s) so docvortex's own timeout fires first and surfaces a typed error.

There is no inline mode: rendering always runs in a `spawn`-context child process. The desktop
`-c` interpreter shim in `__main__.py` already exists for exactly this, so it stays. Step 1
measures peak memory on the Mac with `MINERU_PROCESSING_WINDOW_SIZE=1` and one render process.
If an inline mode matters later, file it upstream and link it here.

## Bucketing redesign

Keep buckets (they give the watchdog a per-20-page timeout and progress logging) but implement
them with MinerU's own `page_range` (`"1-20"`, one-based, inclusive) on the original file instead
of PyMuPDF-split files. Consequences:

- `page_idx` in every output is absolute, so `_image_page_map` and layout blocks need only
  `+1`; `_bucket_char_to_page` still needs the markdown `cursor` offset for the concatenated
  content.
- MinerU re-reads the whole PDF per bucket and rewrites it to the retained pages internally
  (`_maybe_adjust_pdf_bytes`); for 500-page files that is a few hundred ms each — acceptable.
  **verify** on the largest PDFs in the quality corpus.
- MinerU additionally windows internally at `MINERU_PROCESSING_WINDOW_SIZE` (64). Our 20-page
  buckets stay smaller than that, so behaviour is unchanged.

## Quality gate (decides whether this ships)

Corpus: ~200 PDFs copied read-only from saturn's originals across the papers, business and
scanned folders (same folders the embedding test used), plus the 10 slowest PDFs by recorded
`parse_time_seconds` in saturn's `files` table and every PDF that errored there.

Run both parsers on the same machines — moon (CUDA) and this Mac (MPS and, with
`MINERU_MODEL_SMALL_BACKEND=onnx`, CPU) — via `scripts/pdf_parse_compare.py` (new, lives in the
repo, not a one-off): for each PDF record markdown length, whitespace-normalised text similarity
(`difflib.SequenceMatcher` ratio) between 3.x and 4.x, image count, number of char→page anchors,
layout block count by type, parse time, peak RSS and peak VRAM.

Pass criteria:

- Median text similarity ≥ 0.95; no more than 5 % of files below 0.85; every file below 0.85
  reviewed by eye and explained (expected: OCR changes on scans, table re-rendering).
- Image count within ±10 % overall; spot-check 20 files that the figure→page mapping is right.
- No new failures on files that parsed under 3.x; files that failed under 3.x are reported.
- Median parse time not more than 1.5× 3.x on CUDA; MPS and CPU numbers recorded (no gate).
- Peak VRAM on moon within the `MinerU GPU budget` the app allows beside the embedders.
- Loguru sink intact after a parse; no MinerU output on stderr of the parent.

If the gate fails: the Gemma migration proceeds with its fallback (MinerU 3 in a separate
virtualenv), and this plan waits for a later MinerU 4.x.

## Steps

1. **Verify (half a day, on moon, in a throwaway venv).** Install `mineru[torch]==4.0.12`;
   `mineru-kit models download --tier basic --small-backend torch`; parse the fixture PDF with
   `MinerUParser(tier="basic", image_analysis=False)`, `save()`, reload + `render_content_list()`;
   inspect the exact files, keys, `img_path` form and bbox scale; confirm `page_range` →
   absolute `page_idx` (the code passes `page_index_map` into `ModelJson`; confirm the middle
   JSON pages carry original indices); confirm the model singleton survives across two
   `parse()` calls (second call much faster); parse a password-protected PDF and record the
   exception class and `code`; confirm `image_analysis=True` on `basic` is ignored rather than
   an error; confirm `DOCVORTEX_PDF_RENDER_THREADS=1` yields one child process; check
   `python -m mineru.kit.main`; record `pip freeze`. Update every **verify** in this document.
   (Already confirmed from source: env overrides map greedily by field path, so
   `MINERU_MODEL_BASE_DIR` → `model.base_dir`, `MINERU_MODEL_SMALL_BACKEND` → `model.small_backend`;
   page ranges are 1-based inclusive, `"1-20"`, out-of-bounds pages silently omitted.)
2. **Branch `mineru4`** from saturn's `6ede80b`.
3. **`mineru_result.py` + fixtures + unit tests** (pure code, no MinerU needed in CI).
4. **Subprocess + daemon + pdf_parser rewrite**, protocol tests updated.
5. **Config, layout, settings overview, API docstrings.**
6. **Dependencies: pyproject, constraints, Dockerfile, prewarm.** Build the Docker image; run
   the full test suite with `VOITTA_USE_FAKE_PDF_PARSER` off for the real-MinerU test on moon.
7. **Quality gate** with `pdf_parse_compare.py`; attach the summary table to the PR.
8. **Desktop**: lock file regeneration, launcher env, installer prewarm; build the app on the
   Mac; parse the fixture PDF through the packaged app (this is where spawn/`-m` problems show).
9. **Docs.**
10. Rebase `gemma` onto `mineru4`.

## Risks

- MinerU 4 is 16 days old and already at 4.0.12; the changelog notes breaking changes between
  4.0 alphas and release. Pin `<4.1` and read the changelog before any bump.
- `gradio>=6.8` as a hard dependency of the base package adds weight to the Docker image and the
  desktop lock; check it does not pull a conflicting `fastapi`/`pydantic` (we pin
  `fastapi>=0.115`, `pydantic>=2.9`; MinerU needs `pydantic>=2.12.5`).
- OCR language: 3.4 removed the English OCR model selection in favour of the multilingual `ch`
  model. The quality gate's scanned-PDF subset is what catches a regression here.
- `basic` tier quality vs 3.x `pipeline` is the same model family but not identical weights;
  only the gate can say.
- `page_range` parsing rewrites PDF bytes per bucket; a malformed PDF that PyMuPDF tolerated
  may fail in pypdfium2 5.x. The gate includes every PDF that errored on saturn for this reason.
