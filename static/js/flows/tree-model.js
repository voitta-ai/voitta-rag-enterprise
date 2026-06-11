// Pure tree-shape computations.
//
// Everything here is a side-effect-free function over plain data:
// build a path-trie from the files list, walk it for aggregate stats,
// derive which folders are currently "active" (have queued/running
// jobs). Tests can exercise the model without any DOM or store.

import { getGhostDirs } from "./selection.js";

export function aggregateStatus(folderFiles) {
    if (folderFiles.length === 0) return "none";
    if (folderFiles.some((f) => f.state === "error")) return "error";
    if (folderFiles.every((f) => f.state === "indexed" || f.state === "unsupported")) return "indexed";
    return "indexing";
}

// Collapse the indexer's internal substate vocabulary into a small set of
// user-facing labels. ``unsupported`` is its own bucket so we can show it
// differently from a real failure — and from a still-in-progress file.
export function userStateLabel(state) {
    if (state === "indexed" || state === "error" || state === "deleted" || state === "unsupported") return state;
    return "indexing";
}

// Maps a Drive-native ``source_url`` to one of the doc-stem dir
// kinds we render with a Google Workspace icon. URLs follow
// ``https://docs.google.com/{route}/d/<id>/...``; anything else
// (raw file, Drive sharing link, external) returns null.
const _GOOGLE_ROUTES = [
    ["/document/", "document"],
    ["/spreadsheets/", "spreadsheet"],
    ["/presentation/", "presentation"],
    ["/drawings/", "drawing"],
    ["/forms/", "form"],
];

function _googleKindFromUrl(url) {
    if (!url || typeof url !== "string") return null;
    for (const [needle, kind] of _GOOGLE_ROUTES) {
        if (url.includes(needle)) return kind;
    }
    return null;
}

// Tag a dir as a Google Workspace doc-stem iff at least one of its
// *direct* .md children was exported from a Google native source.
// No upward inheritance — an ancestor folder containing a doc-stem
// subdir stays a folder. A doc-stem's own ``images/`` subdir also
// stays a folder; only the .md-bearing dir wears the brand glyph.
function _tagGoogleKinds(node) {
    for (const child of node.dirs.values()) {
        _tagGoogleKinds(child);
    }
    for (const f of node.files) {
        if (!f.rel_path.toLowerCase().endsWith(".md")) continue;
        const k = _googleKindFromUrl(f.source_url);
        if (k) { node.kind = k; return; }
    }
}

export function buildTree(folderFiles, folderId) {
    /* Returns { dirs: Map<name, node>, files: [], kind? } */
    const root = { dirs: new Map(), files: [] };
    for (const f of folderFiles) {
        if (f.state === "deleted") continue;
        const parts = f.rel_path.split("/").filter(Boolean);
        let node = root;
        for (let i = 0; i < parts.length - 1; i++) {
            const part = parts[i];
            if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
            node = node.dirs.get(part);
        }
        node.files.push(f);
    }
    // Merge in any ghost (mkdir-created) directories.
    const ghosts = getGhostDirs().get(folderId);
    if (ghosts) {
        for (const relDir of ghosts) {
            const parts = relDir.split("/").filter(Boolean);
            let node = root;
            for (const part of parts) {
                if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
                node = node.dirs.get(part);
            }
        }
    }
    _tagGoogleKinds(root);
    return root;
}

// NOTE: the old ``activeFolderIds(files, jobs)`` derivation was removed
// in favour of the server-pushed ``activeFolders`` store in store.js.
// Deriving from the SPA's local jobs store broke on deep queues — that
// store is sliced to 50 entries in ws.js, so on a bulk reindex the vast
// majority of queued extracts never appeared in it and folders showed
// as 'indexed' while thousands of their files were still pending. See
// services/folder_active.py for the server-side counter.

export function summariseSubtree(node, folderActive, folderSyncing = false) {
    /* Aggregates file totals across the subtree rooted at node.

       ``folderActive`` is true when the queue currently has at least one
       job touching this subtree's folder. We require BOTH that signal
       AND a non-terminal file in the subtree to render 'indexing' —
       neither alone is sufficient (queue empty → stragglers; folder
       active but all this subtree's files are indexed → another subtree
       is the one moving).

       ``folderSyncing`` is true while the folder's sync source is in
       sync_status == "syncing" (passed for ROOT rows only). It overrides
       every status except 'error': a sync can run with ZERO queued jobs
       (e.g. Google Drive materializing its tree slowly), and showing
       'indexed' mid-sync is exactly the confusion this exists to fix. */
    let total = 0, indexed = 0, unsupported = 0, errored = 0, pending = 0, embedding = 0;
    function walk(n) {
        for (const f of n.files) {
            total++;
            if (f.state === "indexed") indexed++;
            else if (f.state === "unsupported") unsupported++;
            else if (f.state === "error") errored++;
            else if (f.state === "extracted" || f.state === "embedding" || f.pending_embeds > 0) embedding++;
            else pending++;
        }
        for (const child of n.dirs.values()) walk(child);
    }
    walk(node);
    let status = "none";
    if (total > 0) {
        if (errored > 0) status = "error";
        else if (indexed + unsupported === total) status = "indexed";
        else if (folderActive && (embedding > 0 || pending > 0)) status = "indexing";
        // No active jobs but some files are non-terminal. With the
        // server-pushed activeFolders set this branch is now reliable:
        // it means there's truly nothing in flight for this folder
        // (e.g. crash stragglers, files that were skipped because the
        // worker is on another folder, or an admin paused the queue).
        // Render 'pending' (warning yellow) — accurate and visually
        // distinct from both 'indexing' (active) and 'indexed' (done).
        else if (embedding > 0 || pending > 0) status = "pending";
        else status = "indexed";
    }
    if (folderSyncing && status !== "error") status = "syncing";
    return { total, indexed, unsupported, errored, status };
}
