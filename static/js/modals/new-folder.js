// "Create managed folder" modal.
//
// Tiny: name input + Create button. The folder lands under the
// configured ``VOITTA_ROOT_PATH``; if that env var isn't set, the
// button is gated and we surface an alert instead of opening the modal
// (folders can't materialise without a root). The folder list refresh
// is implicit — the backend emits ``folder.added`` over the WS as soon
// as the row is committed.

import { api } from "../api.js";

const $ = (sel) => document.querySelector(sel);

// Server-reported root configuration (path, configured flag). Stashed
// here by ``setRootInfo`` once the bootstrap fetch completes.
let rootInfo = { configured: false, root_path: null };

export function setRootInfo(info) {
    rootInfo = info;
}

export function openModal() {
    if (!rootInfo.configured) {
        alert("Set VOITTA_ROOT_PATH in .env to create new folders.");
        return;
    }
    $("#modal-backdrop").hidden = false;
    $("#modal-root").textContent = rootInfo.root_path;
    $("#managed-name").value = "";
    $("#managed-name").focus();
}

export function closeModal() {
    $("#modal-backdrop").hidden = true;
}

$("#managed-create").addEventListener("click", async () => {
    const name = $("#managed-name").value.trim();
    if (!name) return;
    try {
        await api.addFolderByName(name);
        closeModal();
    } catch (err) { alert(err.message); }
});
