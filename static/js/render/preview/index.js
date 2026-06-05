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
