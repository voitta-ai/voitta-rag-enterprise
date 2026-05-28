// Preview plugin: multi-page documents (PDF, PPTX, DOCX, etc.).
//
// Three display modes, selected by opts.type passed from the sidebar:
//   (none)    — vertical scroll of page renders (default)
//   "images"  — figure gallery: all extracted images as a grid
//   "layout"  — layout view: blocks grouped by page, with type icons + text

import { api } from "../../../api.js";
import { registerPlugin } from "../index.js";

const PAGE_EXTS = new Set([
    ".pdf", ".pptx", ".ppt", ".docx", ".doc",
    ".odp", ".odt",
]);

let _abortCtrl = null;
let _activeFileId = null;
let _activeMode = null; // null | "images" | "layout"

const plugin = {
    canPreview(file) {
        return PAGE_EXTS.has(_ext(file.rel_path));
    },

    async mount(container, file, opts = {}) {
        _activeFileId = file.id;
        _activeMode = opts.type ?? null;
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        container.classList.add("preview-pages");
        container.innerHTML = '<p class="preview-loading">Loading…</p>';

        try {
            if (_activeMode === "images") {
                await _mountImageGallery(container, file, signal);
            } else if (_activeMode === "layout") {
                await _mountLayoutView(container, file, signal);
            } else {
                await _mountPageRenders(container, file, signal);
            }
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">${err.message}</p>`;
        }
    },

    // Called when same file is re-selected. Returns true if a full re-mount
    // is needed (mode changed), false if nothing to do.
    jumpTo(_container, opts) {
        const newMode = opts?.type ?? null;
        return newMode !== _activeMode;
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
        _activeFileId = null;
        _activeMode = null;
        container.classList.remove("preview-pages");
        container.innerHTML = "";
    },
};

// ---------------------------------------------------------------------------
// Page renders (default mode)
// ---------------------------------------------------------------------------

async function _mountPageRenders(container, file, signal) {
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
            if (pg.width && pg.height) { img.width = pg.width; img.height = pg.height; }
            wrapper.append(img);
            container.append(wrapper);
        }
    } else {
        await _mountText(container, file, signal);
    }
}

// ---------------------------------------------------------------------------
// Images gallery mode
// ---------------------------------------------------------------------------

async function _mountImageGallery(container, file, signal) {
    const imgs = await api.fileImages(file.id);
    if (signal.aborted) return;

    container.innerHTML = "";
    if (imgs.length === 0) {
        container.innerHTML = '<p class="preview-hint">No extracted images for this file.</p>';
        return;
    }

    const grid = document.createElement("div");
    grid.className = "artifact-gallery";
    for (const img of imgs) {
        const card = document.createElement("div");
        card.className = "artifact-gallery-card";
        const el = document.createElement("img");
        el.className = "artifact-gallery-img";
        el.loading = "lazy";
        el.decoding = "async";
        el.alt = img.page ? `Figure p.${img.page}` : "Figure";
        el.src = `/api/images/${img.image_id}`;
        const caption = document.createElement("span");
        caption.className = "artifact-gallery-caption";
        caption.textContent = img.page ? `p.${img.page}` : "";
        card.append(el, caption);
        grid.append(card);
    }
    container.append(grid);
}

// ---------------------------------------------------------------------------
// Layout view mode
// ---------------------------------------------------------------------------

const _LAYOUT_ICONS = {
    title: "T",
    text: "¶",
    table: "⊞",
    image: "🖼",
    equation: "∑",
    header: "H",
    page_number: "#",
    page_footnote: "†",
};

async function _mountLayoutView(container, file, signal) {
    const blocks = await api.fileLayout(file.id);
    if (signal.aborted) return;

    container.innerHTML = "";
    if (blocks.length === 0) {
        container.innerHTML = '<p class="preview-hint">No layout data for this file.</p>';
        return;
    }

    // Group blocks by page.
    const byPage = new Map();
    for (const blk of blocks) {
        if (!byPage.has(blk.page)) byPage.set(blk.page, []);
        byPage.get(blk.page).push(blk);
    }

    for (const [page, pageBlocks] of [...byPage.entries()].sort((a, b) => a[0] - b[0])) {
        const section = document.createElement("div");
        section.className = "layout-page-section";

        const header = document.createElement("div");
        header.className = "layout-page-header";
        header.textContent = `Page ${page}`;
        section.append(header);

        for (const blk of pageBlocks) {
            const row = document.createElement("div");
            row.className = `layout-block layout-block-${blk.type}`;

            const icon = document.createElement("span");
            icon.className = "layout-block-icon";
            icon.textContent = _LAYOUT_ICONS[blk.type] || "·";

            const text = document.createElement("span");
            text.className = "layout-block-text";
            text.textContent = blk.text || `(${blk.type})`;

            row.append(icon, text);
            section.append(row);
        }
        container.append(section);
    }
}

// ---------------------------------------------------------------------------
// Text fallback
// ---------------------------------------------------------------------------

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
    } catch {
        if (signal.aborted) return;
        container.innerHTML = '<p class="preview-hint">No preview available — file not yet indexed.</p>';
    }
}

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
