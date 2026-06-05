// Pluggable file preview registry.
//
// Plugins register via registerPlugin(). Plugins are tried in registration
// order; the first whose canPreview() returns true is mounted.
//
// Plugin contract:
//   canPreview(file)            → bool          (sync, called on every selection change)
//   mount(container, file)      → void          (may be async internally; owns container DOM)
//   unmount(container)          → void          (cancel fetches, dispose GL, clear DOM)

import { api } from "../../api.js";
import { files } from "../../store.js";

const $ = (sel) => document.querySelector(sel);

const _plugins = [];
let _activePlugin = null;
let _activeFileId = null;

export function registerPlugin(plugin) {
    _plugins.push(plugin);
}

// Render the per-file provenance rows above the preview body. Each row hides
// when its value is absent; the whole block hides when nothing is known.
function _fmtDate(epoch_s) {
    if (!epoch_s) return "";
    try {
        return new Date(epoch_s * 1000).toLocaleDateString(undefined,
            { year: "numeric", month: "short", day: "numeric" });
    } catch { return ""; }
}

function _person(name, email) {
    if (name && email) return `${name} <${email}>`;
    return name || email || "";
}

function _renderPreviewMeta(file) {
    const block = $("#preview-meta");
    const p = file.provenance || {};
    // Dates: source-provided, with filesystem-mtime fallback for modified and
    // File.added_at as the "Indexed" (entered-our-system) date.
    const modifiedTs = p.modified_ts
        || (file.mtime_ns ? Math.floor(file.mtime_ns / 1e9) : 0);
    const owner = _person(p.owner_name, p.owner_email);
    const editor = _person(p.editor_name, p.editor_email);
    const rows = [
        ["pm-owner", owner],
        // "Modified by" only when the last editor differs from the owner —
        // dropping the noisy duplicate (most files: owner edited it last).
        ["pm-editor", editor && editor !== owner ? editor : ""],
        ["pm-shared", _person(p.shared_by_name, p.shared_by_email)],
        ["pm-created", _fmtDate(p.created_ts)],
        ["pm-modified", _fmtDate(modifiedTs)],
        ["pm-uploaded", _fmtDate(file.added_at)],
    ];
    let anyShown = false;
    for (const [id, val] of rows) {
        const v = $(`#${id}`), k = $(`#${id}-k`);
        const show = !!val;
        v.textContent = val || "–";
        v.title = val || "";
        v.hidden = !show;
        if (k) k.hidden = !show;
        if (show) anyShown = true;
    }
    block.hidden = !anyShown;
}

export function renderFilePreview(fileId, opts = {}) {
    const file = files.get().find((f) => f.id === fileId);
    if (!file) return;

    // Show preview pane, hide folder views.
    $("#sidebar-empty").hidden = true;
    $("#folder-detail").hidden = true;
    $("#file-preview").hidden = false;

    // Update header.
    const basename = file.rel_path.split("/").pop();
    setIfChanged($("#preview-filename"), "textContent", basename);
    const dlBtn = $("#preview-download");
    const dlUrl = api.fileDownloadUrl(file.id);
    if (dlBtn.href !== dlUrl) dlBtn.href = dlUrl;
    if (dlBtn.download !== basename) dlBtn.download = basename;

    _renderPreviewMeta(file);

    // Same file — let the plugin decide if it needs a full re-mount.
    if (fileId === _activeFileId) {
        if (_activePlugin?.jumpTo) {
            const needsRemount = _activePlugin.jumpTo($("#preview-body"), opts);
            if (!needsRemount) return;
            // Plugin signalled mode change — fall through to full re-mount.
            _activePlugin.unmount($("#preview-body"));
            _activePlugin = null;
            _activeFileId = null;
        } else {
            return;
        }
    }

    // Unmount the previous plugin.
    if (_activePlugin) {
        _activePlugin.unmount($("#preview-body"));
        _activePlugin = null;
    }
    _activeFileId = fileId;

    const body = $("#preview-body");
    body.innerHTML = "";

    const plugin = _plugins.find((p) => p.canPreview(file));
    if (plugin) {
        _activePlugin = plugin;
        plugin.mount(body, file, opts);
    }
}

export function unmountPreview() {
    if (_activePlugin) {
        _activePlugin.unmount($("#preview-body"));
        _activePlugin = null;
    }
    _activeFileId = null;
}

function setIfChanged(el, prop, val) {
    if (el && el[prop] !== val) el[prop] = val;
}
