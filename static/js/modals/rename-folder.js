// "Rename folder" modal.
//
// Two fields: the display label (cosmetic — ``folders.display_name``) and
// the physical directory name under ``VOITTA_ROOT_PATH``. The display name
// is always sent; the directory name is only sent when it actually changed,
// so a label-only edit never touches disk. The backend rejects a physical
// rename while indexing/sync jobs are in flight (409) — we surface that
// message verbatim. The folder-list refresh is implicit: the backend emits
// ``folder.upserted`` over the WS once the row is committed (see ws.js),
// and the optimistic merge below just removes the one-roundtrip flicker.

import { api } from "../api.js";
import { folders } from "../store.js";

const $ = (sel) => document.querySelector(sel);

// Server-reported root configuration, stashed by ``setRenameRootInfo`` once
// the bootstrap fetch completes (same source as the New-folder modal).
let rootInfo = { configured: false, root_path: null };
let current = null;        // the folder being renamed
let currentDirName = "";   // its physical basename, for change detection

export function setRenameRootInfo(info) {
    rootInfo = info;
}

export function openRenameModal(folder) {
    if (!folder) return;
    current = folder;
    // ``path`` is absolute (root/<name>); the basename is the dir name we
    // diff against to decide whether a physical rename was requested.
    currentDirName = (folder.path || "").split("/").filter(Boolean).pop() || "";
    $("#rename-display").value = folder.display_name || "";
    $("#rename-dir").value = currentDirName;
    // No root configured → a physical rename is impossible; lock the field
    // and let the user edit the label only.
    $("#rename-dir").disabled = !rootInfo.configured;
    $("#rename-root").textContent = rootInfo.root_path || "(VOITTA_ROOT_PATH not set)";
    $("#rename-backdrop").hidden = false;
    $("#rename-display").focus();
}

export function closeRenameModal() {
    $("#rename-backdrop").hidden = true;
    current = null;
}

$("#rename-save").addEventListener("click", async () => {
    if (!current) return;
    const display = $("#rename-display").value.trim();
    const dir = $("#rename-dir").value.trim();
    if (!display) {
        alert("Display name can't be empty.");
        return;
    }
    // Only send a physical name when it actually changed — otherwise leave
    // it undefined so JSON.stringify drops it and the backend treats the
    // request as label-only.
    const name = dir && dir !== currentDirName ? dir : undefined;
    try {
        const updated = await api.renameFolder(current.id, { display_name: display, name });
        folders.update((list) => list.map((f) => (f.id === updated.id ? updated : f)));
        closeRenameModal();
    } catch (err) {
        alert(err.message);
    }
});

$("#rename-close").addEventListener("click", closeRenameModal);
$("#rename-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "rename-backdrop") closeRenameModal();
});
