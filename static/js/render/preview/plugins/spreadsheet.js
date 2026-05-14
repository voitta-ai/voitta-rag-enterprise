// Preview plugin: spreadsheets (XLSX, XLSM, XLS, ODS).
//
// The XLSX parser extracts each sheet as a pipe-style markdown table
// under a `## Sheet: <name>` heading. We render that markdown so the
// user sees a real table per sheet instead of raw `| col | col |` text.

import { registerPlugin } from "../index.js";
import { renderMarkdownInto } from "../markdown.js";

const SHEET_EXTS = new Set([".xlsx", ".xlsm", ".xls", ".ods"]);

let _abortCtrl = null;

const plugin = {
    canPreview(file) {
        return SHEET_EXTS.has(_ext(file.rel_path));
    },

    async mount(container, file) {
        container.classList.add("preview-spreadsheet");
        container.innerHTML = '<p class="preview-loading">Loading sheets…</p>';
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        try {
            const resp = await fetch(`/api/files/${file.id}/text`, {
                credentials: "same-origin",
                signal,
            });
            if (signal.aborted) return;
            if (!resp.ok) {
                if (resp.status === 409) {
                    container.innerHTML = '<p class="preview-hint">File not yet indexed.</p>';
                    return;
                }
                throw new Error(`${resp.status} ${resp.statusText}`);
            }
            const text = await resp.text();
            if (signal.aborted) return;
            container.innerHTML = "";
            await renderMarkdownInto(container, text);
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">${err.message}</p>`;
        }
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
        container.classList.remove("preview-spreadsheet");
        container.innerHTML = "";
    },
};

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
