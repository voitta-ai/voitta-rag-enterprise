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

## simple/ — mixed brand-icon set

This directory is named after our original Simple Icons source but
now mixes three providers. All are individually attributed:

* **Simple Icons** (CC0 1.0 — `simple/LICENSE.md`)
  Source: https://github.com/simple-icons/simple-icons @ `09e0842`
  Files: `freecad.svg`, `dropbox.svg`, `googlecloud.svg`.

* **Wikimedia Commons / public-domain Google logos**
  Source: https://commons.wikimedia.org/wiki/File:Google_Drive_icon_(2020).svg
  and the matching `Google_{Docs,Sheets,Slides,Forms}_2020_Logo.svg`
  pages. These are released by Google as trademarks and are
  reproduced here under fair use as application icons. Files:
  `googledrive.svg`, `googledocs.svg`, `googlesheets.svg`,
  `googleslides.svg`, `googleforms.svg`. The multi-colour brand
  versions — `simple-icons` only ships single-fill paths.

* **Lobe Icons** (MIT) for the GitHub octocat.
  Source: https://github.com/lobehub/lobe-icons @ `0c66d0a3`.
  Shipped as `github-light.svg` (`fill="#181717"`) and
  `github-dark.svg` (`fill="#f5f5f7"`) — the upstream SVG uses
  `fill="currentColor"` which an `<img>` element can't inherit.

* **Hand-crafted:** `sharepoint.svg` — a white "S" stroke on the official
  SharePoint teal (#038387), drawn as a single cubic-bezier path. Simple Icons
  dropped Microsoft icons in 2024 over brand-guidelines concerns; this is a
  minimal clean-room approximation. Azure and S3 remain absent.

* **Not present:** AWS (S3) and Microsoft Azure — see above.

## Why vendor at all

* No CDN dependency — works in egress-restricted customer deployments.
* No build step — the SVGs are static files served by FastAPI's
  ``StaticFiles`` mount under `/static/icons/...`.
* The icon set we care about is small (~45 files, ~180 KB) — checking
  it in is cheaper than maintaining a fetch script that has to handle
  rate limits and rename detection.
