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

export function buildTree(folderFiles, folderId) {
    /* Returns { dirs: Map<name, node>, files: [] } */
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
    return root;
}

export function activeFolderIds(filesList, jobsList) {
    /* Map queued + running jobs back to folder ids so the per-row
       "indexing" pill only lights up on folders with work actually in
       flight. Previously the check was global ("any job running
       anywhere?") which made every folder containing a stale
       non-terminal file (left behind by a past abandoned job) flash to
       'indexing' the moment another folder's reindex started.

       Job payload shapes (see services/job_queue.py + scanner / indexing):
       - extract / embed_text / delete_file: {file_id}
       - reindex_folder / sync:               {folder_id}
       embed_image runs inline within extract, never queued separately. */
    const fileFolder = new Map();
    for (const f of filesList) fileFolder.set(f.id, f.folder_id);
    const out = new Set();
    for (const j of jobsList) {
        if (j.state !== "queued" && j.state !== "running") continue;
        const p = j.payload || {};
        if (p.folder_id != null) {
            out.add(p.folder_id);
        } else if (p.file_id != null) {
            const fid = fileFolder.get(p.file_id);
            if (fid != null) out.add(fid);
        }
    }
    return out;
}

export function summariseSubtree(node, folderActive) {
    /* Aggregates file totals across the subtree rooted at node.

       ``folderActive`` is true when the queue currently has at least one
       job touching this subtree's folder. We require BOTH that signal
       AND a non-terminal file in the subtree to render 'indexing' —
       neither alone is sufficient (queue empty → stragglers; folder
       active but all this subtree's files are indexed → another subtree
       is the one moving). */
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
        // No active jobs for this folder but some files aren't terminal —
        // they're stragglers, not work in flight. Reading 'indexing' here
        // is a lie; treat the subtree as done so the UI matches the queue.
        else status = "indexed";
    }
    return { total, indexed, unsupported, errored, status };
}
