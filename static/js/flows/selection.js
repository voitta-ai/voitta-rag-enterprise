// Selection + expansion state.
//
// What's "selected" drives toolbar enablement, sidebar contents, and the
// stats fetch. What's "expanded" drives which subtrees the tree view
// reveals. Both are session-scoped (resets on reload) and module-private:
// callers go through the exported getters / setters so we have a single
// place to add side effects later (URL routing, undo stacks, etc.).
//
// "Ghost dirs" are subdirectories created via the New-subfolder button
// that don't have any files yet — they'd otherwise vanish from the tree
// because tree rendering walks file paths. We keep them in a per-folder
// Set so the tree builder can synthesise empty-dir nodes for them.

import { scheduleFullRender } from "../render/render-loop.js";

let _selectedFolderId = null;
let _selectedRelDir = ""; // "" = folder root; otherwise "subdir/inner"
let _selectedFileId = null; // null = dir/root selected; number = file selected

const _expandedNodes = new Set(); // keys: `${folder_id}:${rel_dir}`
const _expandedFiles = new Set(); // keys: file_id (number) — for artifact children
// null = whole file selected; otherwise { type: 'images'|'layout' }
let _selectedArtifact = null;
const _ghostDirs = new Map(); // folder_id → Set<rel_dir>

export function nodeKey(folderId, relDir) {
    return `${folderId}:${relDir}`;
}

export function getSelectedFolderId() {
    return _selectedFolderId;
}

export function getSelectedRelDir() {
    return _selectedRelDir;
}

export function getSelectedFileId() {
    return _selectedFileId;
}

export function setSelection(folderId, relDir, fileId = null) {
    _selectedFolderId = folderId;
    _selectedRelDir = relDir;
    _selectedFileId = fileId;
}

export function getExpandedNodes() {
    return _expandedNodes;
}

export function isExpanded(folderId, relDir) {
    return _expandedNodes.has(nodeKey(folderId, relDir));
}

export function toggleExpanded(folderId, relDir) {
    const key = nodeKey(folderId, relDir);
    if (_expandedNodes.has(key)) _expandedNodes.delete(key);
    else _expandedNodes.add(key);
}

export function expand(folderId, relDir) {
    _expandedNodes.add(nodeKey(folderId, relDir));
}

export function isFileExpanded(fileId) {
    return _expandedFiles.has(fileId);
}

export function toggleFileExpanded(fileId) {
    if (_expandedFiles.has(fileId)) _expandedFiles.delete(fileId);
    else _expandedFiles.add(fileId);
}

// Returns null (whole file) or { type: 'image'|'layout', index: number }
export function getSelectedArtifact() {
    return _selectedArtifact;
}

export function selectArtifact(folderId, relDir, fileId, type) {
    setSelection(folderId, relDir, fileId);
    _selectedArtifact = { type };
    scheduleFullRender();
}

export function getGhostDirs() {
    return _ghostDirs;
}

export function addGhostDir(folderId, relDir) {
    if (!_ghostDirs.has(folderId)) _ghostDirs.set(folderId, new Set());
    _ghostDirs.get(folderId).add(relDir);
}

export function removeGhostDir(folderId, relDir) {
    const set = _ghostDirs.get(folderId);
    if (!set) return;
    const prefix = relDir.endsWith("/") ? relDir : relDir + "/";
    for (const d of [...set]) {
        if (d === relDir || d.startsWith(prefix)) set.delete(d);
    }
}

// Mutate selection + ask the render loop for a full pass. The render
// loop will rebuild the tree, sidebar, and toolbar in a single rAF
// callback so a click never rebuilds three times.
export function selectNode(folderId, relDir, fileId = null) {
    setSelection(folderId, relDir, fileId);
    _selectedArtifact = null;
    scheduleFullRender();
}

// ---------------------------------------------------------------------------
// Location — which folder the list is showing
// ---------------------------------------------------------------------------
//
// The folder list shows one location at a time: all folders (folderId
// null), or the contents of one folder / subdirectory, with a breadcrumb
// back up. Clicking a folder row opens it; the chevron expands it in
// place. Each move is a browser-history entry, so Back / Forward walk the
// path. Opening a location also selects it, so the sidebar shows its
// details.

let _locFolderId = null;
let _locRelDir = "";

export function getLocation() {
    return { folderId: _locFolderId, relDir: _locRelDir };
}

function _setLocation(folderId, relDir, history) {
    const same = folderId === _locFolderId && relDir === _locRelDir;
    _locFolderId = folderId;
    _locRelDir = folderId === null ? "" : relDir;
    const state = { voittaLocation: { folderId: _locFolderId, relDir: _locRelDir } };
    if (history === "push" && !same) window.history.pushState(state, "");
    else if (history === "replace") window.history.replaceState(state, "");
}

// ``history``: "push" (a user move — Back returns here), "replace" (the
// current location is gone, e.g. it was deleted) or "none" (restoring an
// entry on Back / Forward).
export function openLocation(folderId, relDir = "", { history = "push" } = {}) {
    _setLocation(folderId, relDir, history);
    selectNode(_locFolderId, _locRelDir);
}

// Show a file's directory with the file selected (e.g. a citation link).
export function openFileLocation(folderId, relDir, fileId) {
    _setLocation(folderId, relDir, "push");
    selectNode(folderId, relDir, fileId);
}

// The open folder disappeared (deleted, or access revoked): back to all
// folders. Called from inside a render, so it neither selects nor schedules.
export function leaveMissingLocation() {
    const gone = _locFolderId;
    _setLocation(null, "", "replace");
    if (_selectedFolderId === gone) setSelection(null, "");
}

// Rewrite the initial history entry so Back to it lands on "all folders".
export function initLocationHistory() {
    _setLocation(null, "", "replace");
}
