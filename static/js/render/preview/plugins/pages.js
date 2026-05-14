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

const PAGE_EXTS = new Set([
    ".pdf", ".pptx", ".ppt", ".docx", ".doc",
    ".xlsx", ".xls", ".odp", ".ods", ".odt",
]);

let _abortCtrl = null;

const plugin = {
    canPreview(file) {
        return PAGE_EXTS.has(_ext(file.rel_path));
    },

    async mount(container, file) {
        container.classList.add("preview-pages");
        container.innerHTML = '<p class="preview-loading">Loading pages…</p>';
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        try {
            const pages = await api.filePageImages(file.id);
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
                    container.append(wrapper);
                }
            } else {
                // No page renders — fall back to extracted text.
                await _mountText(container, file, signal);
            }
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">${err.message}</p>`;
        }
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
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

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
