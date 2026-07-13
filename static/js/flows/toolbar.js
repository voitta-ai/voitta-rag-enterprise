// Top toolbar buttons + per-folder actions.
//
// Owns ``updateToolbarState`` (called from the render loop) and wires
// every toolbar button click. Modal openers (New folder, Sync) come
// from their respective modal modules; this module just wires the
// click handlers.

import { api } from "../api.js";
import { renderFolders } from "../render/tree.js";
import { closeModal, openModal } from "../modals/new-folder.js";
import { openRenameModal } from "../modals/rename-folder.js";
import { addGhostDir, expand, getSelectedFileId, getSelectedFolderId, getSelectedRelDir, removeGhostDir, setSelection } from "./selection.js";
import { scheduleFullRender } from "../render/render-loop.js";
import { files, folders, jobs } from "../store.js";

const $ = (sel) => document.querySelector(sel);

export function updateToolbarState() {
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    const isRoot = !!folder && getSelectedRelDir() === "";
    // Read-only = a shared folder owned by someone else. Owner-only mutations
    // (upload, mkdir, reindex, sync, remove) are disabled; viewers can still
    // expand the tree and read files.
    const isOwned = !!(folder && folder.owned);
    const readOnly = !!folder && !isOwned;
    // Synced folders (git, google drive, …) mirror a remote source: their
    // contents are owned by the sync, so manual uploads/mkdir make no sense
    // (the next sync would clobber or orphan them). Disable those buttons
    // and explain why in the tooltip.
    const isRegular = (folder?.sync_source_kind || "regular") === "regular";
    const syncBlocked = !!folder && !isRegular;
    const syncedTip = "Disabled — this folder is synced from a remote source " +
        "(Git, Google Drive, …). Add files at the source and re-sync instead.";

    $("#btn-new-subfolder").disabled = !folder || readOnly || syncBlocked;
    $("#btn-new-subfolder").title = syncBlocked
        ? syncedTip
        : "Create a subfolder inside the selected managed folder";
    $("#btn-upload").disabled = !folder || readOnly || syncBlocked;
    $("#btn-upload").title = syncBlocked
        ? syncedTip
        : "Upload one or more files into the selected managed folder";
    $("#btn-upload-folder").disabled = !folder || readOnly || syncBlocked;
    $("#btn-upload-folder").title = syncBlocked
        ? syncedTip
        : "Upload a folder (with its full subdirectory tree preserved)";
    $("#btn-reindex").disabled = !folder || readOnly;
    // Sync button: only at the folder root. Hidden when the folder is
    // non-empty AND has no sync source — sync can't be configured on an
    // existing folder of files. When a sync source already exists, the
    // same button reads "Config" (re-opens the same modal).
    const syncBtn = $("#btn-sync");
    if (!folder || !isRoot || readOnly) {
        syncBtn.hidden = true;
        syncBtn.disabled = true;
    } else {
        const hasSync = !!folder.has_sync_source;
        const folderFiles = files.get().filter(
            (x) => x.folder_id === folder.id && x.state !== "deleted",
        );
        const isEmpty = folderFiles.length === 0;
        if (!hasSync && !isEmpty) {
            syncBtn.hidden = true;
            syncBtn.disabled = true;
        } else {
            // The HTML starts with `disabled` so the button doesn't flicker
            // before the first updateToolbarState — clear it whenever the
            // button is meant to be usable.
            syncBtn.hidden = false;
            syncBtn.disabled = false;
            syncBtn.textContent = hasSync ? "🔄 Config" : "🔄 Sync";
            syncBtn.title = hasSync
                ? "Edit the remote sync configuration for this folder"
                : "Configure a remote sync (e.g. GitHub) for this empty folder";
        }
    }
    const selectedFileId = getSelectedFileId();
    const selectedRelDir = getSelectedRelDir();
    // Remove enabled for: top-level folder (owner only), subdir (owned regular), file (owned regular)
    const canRemove = !!folder && isOwned && (
        isRoot ||
        (!selectedFileId && selectedRelDir && isRegular) ||
        (!!selectedFileId && isRegular)
    );
    $("#btn-remove").disabled = !canRemove;
    // Rename is a top-level, owner-only action (it can move the directory
    // on disk + relabel the folder). Disabled for subdirs, files, and
    // read-only (someone else's shared) folders.
    $("#btn-rename").disabled = !folder || !isOwned || !isRoot;
}

async function createSubfolder() {
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    if (!folder) return;
    const name = prompt("Subfolder name:");
    if (!name?.trim()) return;
    const target = getSelectedRelDir()
        ? `${getSelectedRelDir()}/${name.trim()}`
        : name.trim();
    try {
        await api.mkdir(folder.id, target);
        addGhostDir(folder.id, target);
        expand(folder.id, getSelectedRelDir());
        renderFolders(folders.get());
    } catch (err) {
        alert(err.message);
    }
}

// ---------------------------------------------------------------------------
// Module-load wiring
// ---------------------------------------------------------------------------

$("#btn-new-folder").addEventListener("click", openModal);
$("#modal-close").addEventListener("click", closeModal);
$("#modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
});

$("#btn-new-subfolder").addEventListener("click", createSubfolder);

// ---------------------------------------------------------------------------
// Reindex scope chooser — opened by the Reindex button. Three checkboxes
// map onto the endpoint's ``states`` filter: errored / skipped map 1:1,
// "healthy" covers everything else non-deleted. All three checked = a
// full reindex (states=null). Default is errored+skipped: the common case
// after a parser fix is retrying failures, not re-parsing the corpus.
// ---------------------------------------------------------------------------

const _HEALTHY_STATES = ["indexed", "pending", "extracted", "embedding"];

function _reindexSubtreeFiles() {
    const folderId = getSelectedFolderId();
    const relDir = getSelectedRelDir();
    const all = files.get().filter(
        (x) => x.folder_id === folderId && x.state !== "deleted",
    );
    return relDir ? all.filter((f) => f.rel_path.startsWith(`${relDir}/`)) : all;
}

function _reindexCounts() {
    const counts = { error: 0, unsupported: 0, healthy: 0 };
    for (const f of _reindexSubtreeFiles()) {
        if (f.state === "error") counts.error += 1;
        else if (f.state === "unsupported") counts.unsupported += 1;
        else counts.healthy += 1;
    }
    return counts;
}

// The states array the current checkbox combination selects, or null for
// a full reindex, or [] when nothing is checked.
function _reindexSelectedStates() {
    const states = [];
    if ($("#reindex-scope-error").checked) states.push("error");
    if ($("#reindex-scope-unsupported").checked) states.push("unsupported");
    const healthy = $("#reindex-scope-healthy").checked;
    if (healthy) states.push(..._HEALTHY_STATES);
    if (states.length === 2 + _HEALTHY_STATES.length) return null; // everything
    return states;
}

function _refreshReindexModal() {
    const counts = _reindexCounts();
    $("#reindex-count-error").textContent = String(counts.error);
    $("#reindex-count-unsupported").textContent = String(counts.unsupported);
    $("#reindex-count-healthy").textContent = String(counts.healthy);
    const selected =
        (counts.error && $("#reindex-scope-error").checked ? counts.error : 0) +
        (counts.unsupported && $("#reindex-scope-unsupported").checked ? counts.unsupported : 0) +
        (counts.healthy && $("#reindex-scope-healthy").checked ? counts.healthy : 0);
    const startBtn = $("#reindex-start");
    startBtn.disabled = selected === 0;
    startBtn.textContent = selected ? `Reindex ${selected} file(s)` : "Reindex";
    $("#reindex-scope-hint").textContent = $("#reindex-scope-healthy").checked
        ? "Selected files are re-parsed and re-embedded from scratch; existing "
          + "chunks stay searchable until replaced. Re-parsing the healthy "
          + "corpus can take a long time."
        : "Only failed/skipped files are retried — the healthy corpus is untouched. "
          + "Files that are genuinely unsupported will simply park as skipped again.";
}

$("#btn-reindex").addEventListener("click", () => {
    if (!getSelectedFolderId()) return;
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    if (!folder) return;
    if (_reindexSubtreeFiles().length === 0) {
        alert("No files to reindex in this subtree.");
        return;
    }
    const where = getSelectedRelDir()
        ? `${folder.display_name}/${getSelectedRelDir()}`
        : folder.display_name;
    $("#reindex-where").textContent = `Scope: ${where}`;
    // Reset to the cheap default on every open.
    $("#reindex-scope-error").checked = true;
    $("#reindex-scope-unsupported").checked = true;
    $("#reindex-scope-healthy").checked = false;
    _refreshReindexModal();
    $("#reindex-backdrop").hidden = false;
});

function closeReindexModal() {
    $("#reindex-backdrop").hidden = true;
}

for (const id of ["#reindex-scope-error", "#reindex-scope-unsupported", "#reindex-scope-healthy"]) {
    $(id).addEventListener("change", _refreshReindexModal);
}
$("#reindex-close").addEventListener("click", closeReindexModal);
$("#reindex-cancel").addEventListener("click", closeReindexModal);
$("#reindex-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "reindex-backdrop") closeReindexModal();
});
$("#reindex-start").addEventListener("click", async () => {
    const states = _reindexSelectedStates();
    if (Array.isArray(states) && states.length === 0) return;
    closeReindexModal();
    try {
        const r = await api.reindexFolder(getSelectedFolderId(), getSelectedRelDir(), states);
        if (r.scheduled === 0) alert("No files matched the selected scope.");
    } catch (err) {
        alert(err.message);
    }
});

$("#btn-remove").addEventListener("click", async () => {
    const folderId = getSelectedFolderId();
    if (!folderId) return;
    const folder = folders.get().find((f) => f.id === folderId);
    if (!folder) return;

    const fileId = getSelectedFileId();
    const relDir = getSelectedRelDir();

    try {
        if (fileId) {
            // Delete a single file.
            const fileObj = files.get().find((f) => f.id === fileId);
            const name = fileObj ? fileObj.rel_path.split("/").pop() : `file #${fileId}`;
            if (!confirm(`Delete "${name}"?\n\nThis cannot be undone.`)) return;
            await api.deleteFile(folderId, fileId);
            // Remove from files store immediately; WS event will confirm.
            files.update((list) => list.filter((f) => f.id !== fileId));
            setSelection(folderId, relDir);
            scheduleFullRender();
        } else if (relDir) {
            // Delete a subdirectory.
            const dirName = relDir.split("/").pop();
            if (!confirm(`Delete folder "${dirName}" and all its contents?\n\nThis cannot be undone.`)) return;
            await api.deleteSubdir(folderId, relDir);
            // Remove dir and all its children from ghost dirs, files store.
            removeGhostDir(folderId, relDir);
            const prefix = relDir.endsWith("/") ? relDir : relDir + "/";
            files.update((list) => list.filter(
                (f) => f.folder_id !== folderId || (f.rel_path !== relDir && !f.rel_path.startsWith(prefix))
            ));
            setSelection(folderId, "");
            scheduleFullRender();
        } else {
            // Delete the top-level folder.
            if (!confirm(`Delete folder "${folder.display_name}"?\n\nThe folder and all its files will be permanently removed from disk.`)) return;
            await api.deleteFolder(folderId);
            setSelection(null, "");
        }
    } catch (err) {
        alert(err.message);
    }
});

$("#btn-rename").addEventListener("click", () => {
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    if (!folder) return;
    openRenameModal(folder);
});

$("#btn-retry-all").addEventListener("click", async () => {
    try {
        const r = await api.retryAllFailed();
        if (r.retried === 0) alert("No failed jobs to retry");
    } catch (err) { alert(err.message); }
});
$("#btn-clear-failed").addEventListener("click", async () => {
    if (!confirm("Permanently delete all failed-job records?")) return;
    try {
        await api.cleanupFailedJobs();
        jobs.set(await api.recentJobs());
    } catch (err) { alert(err.message); }
});
$("#btn-kill-all").addEventListener("click", async () => {
    if (!confirm("Stop the running job and discard everything queued?")) return;
    try {
        const r = await api.cancelAllJobs();
        if (r.cancelled_queued === 0 && r.killed_running === 0) {
            alert("No running or queued jobs to kill");
        }
        // Refresh the panel so the user sees queued rows flip to done.
        jobs.set(await api.recentJobs());
    } catch (err) { alert(err.message); }
});
