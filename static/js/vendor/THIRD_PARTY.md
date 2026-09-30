# Vendored third-party browser libraries

Everything under `static/js/vendor/` is served locally so the UI has no
external-CDN dependency (it must work on locked-down and offline networks,
and inside the desktop app). Each file is an unmodified upstream build unless
noted.

| File | Library | Version | License |
|------|---------|---------|---------|
| `marked.js` | [marked](https://github.com/markedjs/marked) (esm.sh bundle) | 13.0.3 | MIT |
| `purify.js` | [DOMPurify](https://github.com/cure53/DOMPurify) `dist/purify.es.mjs` (source-map comment removed) | 3.4.16 | Apache-2.0 OR MPL-2.0 |
| `highlight/highlight.min.js` | [highlight.js](https://github.com/highlightjs/highlight.js) ES build, common languages | 11.12.0 | BSD-3-Clause |
| `mermaid.min.js` | [mermaid](https://github.com/mermaid-js/mermaid) `dist/mermaid.min.js` (UMD, sets `globalThis.mermaid`) — lazy-loaded only when a diagram is rendered | 11.17.2 | MIT |
| `three/` | [three.js](https://github.com/mrdoob/three.js) core + addons | — | MIT |
| `xlsx.js` | [SheetJS CE](https://git.sheetjs.com/sheetjs/sheetjs) (esm.sh bundle) | 0.18.5 | Apache-2.0 |

To upgrade one: `npm pack <name>@<version>`, copy the same `dist` file over
the old one, update the row above, and re-run the preview/assistant checks.
