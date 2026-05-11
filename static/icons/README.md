# Vendored icons

This directory contains a curated subset of two upstream icon libraries.
Files are vendored byte-for-byte; the LICENSE files alongside them are
copies of the upstream licenses.

## material/ — Material Icon Theme (MIT)

* **Source:** https://github.com/PKief/vscode-material-icon-theme
* **Pin:** `4de4acf7dd3ed642144c85153753164ddf7312cf` (main branch as of 2026-05-11)
* **License:** MIT — `material/LICENSE`
* **What's here:** file-type icons (`material/file/`), folder variants
  (`material/folder/`), and the `lock` + `google` glyphs used as
  overlays / data-source badges (`material/source/`).

To refresh: bump the pin in `scripts/refresh_icons.sh` (when added) or
re-run the curl loop documented at the top of `static/js/render/icons.js`,
then audit the diff for any visual regressions.

## simple/ — Simple Icons (CC0)

* **Source:** https://github.com/simple-icons/simple-icons
* **Pin:** `09e084221b9ab229e15b42e035d856e99554a1a4` (develop branch as of 2026-05-11)
* **License:** CC0 1.0 — `simple/LICENSE.md`
* **What's here:** brand-faithful logos in their canonical colours —
  FreeCAD (for `.FCStd`), Google Docs / Sheets / Slides / Forms (for
  doc-stem dirs), Google Drive / GitHub / Dropbox / Google Cloud (for
  top-level data-source badges).
* **Not present:** Microsoft (SharePoint, Azure) and AWS (S3) — Simple
  Icons removed these in 2024 over brand-guidelines concerns. When those
  connectors land we'll vendor from `https://github.com/lobehub/lobe-icons`
  or sketch our own; see the TODO block in `static/js/render/icons.js`.

## Why vendor at all

* No CDN dependency — works in egress-restricted customer deployments.
* No build step — the SVGs are static files served by FastAPI's
  ``StaticFiles`` mount under `/static/icons/...`.
* The icon set we care about is small (~45 files, ~180 KB) — checking
  it in is cheaper than maintaining a fetch script that has to handle
  rate limits and rename detection.
