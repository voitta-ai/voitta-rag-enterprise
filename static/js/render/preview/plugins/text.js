// Preview plugin: plain text, source code, and Markdown.
//
// .md files are rendered as HTML via marked (lazy-loaded from CDN).
// All other text extensions fall back to a plain <pre>.
// The CAS-extracted text endpoint is used so the file doesn't need to
// be on disk (works for Google Drive synced files too).

import { registerPlugin } from "../index.js";
import { renderMarkdownInto } from "../markdown.js";

const MD_EXTS = new Set([".md", ".markdown", ".mdx"]);

const TEXT_EXTS = new Set([
    ".txt", ".rst", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".xml", ".html", ".htm", ".css",
    ".js", ".mjs", ".ts", ".jsx", ".tsx",
    ".py", ".rb", ".php", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".sql", ".r", ".m", ".swift", ".kt",
    ".log", ".diff", ".patch",
    ...MD_EXTS,
]);

let _abortCtrl = null;

const plugin = {
    canPreview(file) {
        return TEXT_EXTS.has(_ext(file.rel_path));
    },

    async mount(container, file) {
        container.classList.add("preview-text-wrap");
        container.innerHTML = '<p class="preview-loading">Loading…</p>';
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        const isMarkdown = MD_EXTS.has(_ext(file.rel_path));

        try {
            const resp = await fetch(`/api/files/${file.id}/text`, {
                credentials: "same-origin",
                signal,
            });
            if (signal.aborted) return;
            if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
            const text = await resp.text();
            if (signal.aborted) return;

            container.innerHTML = "";
            if (isMarkdown) {
                await renderMarkdownInto(container, text);
            } else {
                const pre = document.createElement("pre");
                pre.className = "preview-text";
                pre.textContent = text;
                container.append(pre);
            }
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">${err.message}</p>`;
        }
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
        container.classList.remove("preview-text-wrap");
        container.innerHTML = "";
    },
};

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
