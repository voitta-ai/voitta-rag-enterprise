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

let _selectedFolderId = null;
let _selectedRelDir = ""; // "" = folder root; otherwise "subdir/inner"

const _expandedNodes = new Set(); // keys: `${folder_id}:${rel_dir}`
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

export function setSelection(folderId, relDir) {
    _selectedFolderId = folderId;
    _selectedRelDir = relDir;
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

export function getGhostDirs() {
    return _ghostDirs;
}

export function addGhostDir(folderId, relDir) {
    if (!_ghostDirs.has(folderId)) _ghostDirs.set(folderId, new Set());
    _ghostDirs.get(folderId).add(relDir);
}
