// Preview plugin: multi-page documents (PDF, PPTX, DOCX, XLSX, etc.).
//
// Primary: fetch page_render images via GET /api/files/{id}/page-images,
// render as a long vertical scroll. Each <img> uses native loading="lazy"
// so only visible pages are decoded — IntersectionObserver not needed.
//
// Fallback: if no page images exist (file not yet indexed or format has no
// page renders), show the extracted text from GET /api/files/{id}/text.

import { api } from "../../../api.js";
import { registerPlugin } from "../index.js";

// Per-file image list cache (shared with tree so we don't re-fetch).
// Populated lazily on first mount; cleared when plugin unmounts.
const _fileImagesCache = new Map();

const PAGE_EXTS = new Set([
    ".pdf", ".pptx", ".ppt", ".docx", ".doc",
    ".odp", ".odt",
]);

let _abortCtrl = null;
let _activeFileId = null;

const plugin = {
    canPreview(file) {
        return PAGE_EXTS.has(_ext(file.rel_path));
    },

    async mount(container, file, opts = {}) {
        _activeFileId = file.id;
        container.classList.add("preview-pages");
        container.innerHTML = '<p class="preview-loading">Loading pages…</p>';
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        try {
            const [pages, imgs] = await Promise.all([
                api.filePageImages(file.id),
                _fileImagesCache.has(file.id)
                    ? _fileImagesCache.get(file.id)
                    : api.fileImages(file.id).then((r) => { _fileImagesCache.set(file.id, r); return r; }),
            ]);
            if (signal.aborted) return;

            if (pages.length > 0) {
                container.innerHTML = "";
                for (const pg of pages) {
                    const wrapper = document.createElement("div");
                    wrapper.className = "preview-page-wrapper";
                    const img = document.createElement("img");
                    img.className = "preview-page-img";
                    img.loading = "lazy";
                    img.decoding = "async";
                    img.alt = `Page ${pg.page ?? pg.image_index + 1}`;
                    img.src = `/api/images/${pg.image_id}`;
                    if (pg.width && pg.height) {
                        img.width = pg.width;
                        img.height = pg.height;
                    }
                    wrapper.append(img);
                    // Overlay extracted-figure thumbnails that belong on this page.
                    for (const fi of imgs) {
                        if (fi.page === pg.page) {
                            const fig = document.createElement("img");
                            fig.className = "preview-figure-pin";
                            fig.loading = "lazy";
                            fig.decoding = "async";
                            fig.alt = `Figure (p.${fi.page})`;
                            fig.src = `/api/images/${fi.image_id}`;
                            wrapper.append(fig);
                        }
                    }
                    container.append(wrapper);
                }
                _applyArtifactFocus(container, opts, imgs);
            } else {
                // No page renders — fall back to extracted text.
                await _mountText(container, file, signal);
            }
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">${err.message}</p>`;
        }
    },

    jumpTo(container, opts) {
        const imgs = (_activeFileId != null && _fileImagesCache.get(_activeFileId)) ?? [];
        _applyArtifactFocus(container, opts, imgs);
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
        _activeFileId = null;
        container.classList.remove("preview-pages");
        container.innerHTML = "";
    },
};

async function _mountText(container, file, signal) {
    container.innerHTML = '<p class="preview-loading">Loading text…</p>';
    try {
        const text = await fetch(`/api/files/${file.id}/text`, {
            credentials: "same-origin",
            signal,
        }).then((r) => {
            if (!r.ok) throw new Error(`${r.status}`);
            return r.text();
        });
        if (signal.aborted) return;
        container.innerHTML = "";
        const pre = document.createElement("pre");
        pre.className = "preview-text";
        pre.textContent = text;
        container.append(pre);
    } catch (err) {
        if (signal.aborted) return;
        container.innerHTML = '<p class="preview-hint">No preview available — file not yet indexed.</p>';
    }
}

// Remove previous artifact highlight, apply new one based on opts.
// opts = {} (whole file) | { type: 'image', index } | { type: 'layout', index }
function _applyArtifactFocus(container, opts, imgs) {
    // Clear previous highlight.
    container.querySelectorAll(".preview-figure-pin.highlighted, .preview-page-wrapper.highlighted")
        .forEach((el) => el.classList.remove("highlighted"));

    if (!opts || !opts.type) return;

    if (opts.type === "image" && opts.index != null) {
        const img = imgs[opts.index];
        if (!img) return;
        // Find the pin overlay for this image and highlight + scroll to it.
        const pins = container.querySelectorAll(".preview-figure-pin");
        // Pins are rendered in image-index order per page; find by src.
        const src = `/api/images/${img.image_id}`;
        for (const pin of pins) {
            if (pin.getAttribute("src") === src) {
                pin.classList.add("highlighted");
                pin.scrollIntoView({ behavior: "smooth", block: "center" });
                return;
            }
        }
        // Fallback: scroll to the page the image is on (0-based index in wrappers).
        if (img.page != null) {
            const wrappers = container.querySelectorAll(".preview-page-wrapper");
            for (const w of wrappers) {
                const pageImg = w.querySelector(".preview-page-img");
                if (pageImg?.alt?.endsWith(String(img.page))) {
                    w.scrollIntoView({ behavior: "smooth", block: "start" });
                    return;
                }
            }
        }
    }

    if (opts.type === "layout" && opts.index != null) {
        // Layout blocks are only page-addressable from here — scroll to their page.
        // The block's page is not passed in opts; the caller (sidebar) knows it
        // via the layout array. For now we piggy-back on the layout cache from the
        // tree module by doing nothing special — the user already sees the block
        // label in the tree. A richer highlight would need DOM markers per block.
    }
}

function _scrollToPage(container, pageIndex) {
    const wrappers = container.querySelectorAll(".preview-page-wrapper");
    const target = wrappers[pageIndex];
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
}

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
