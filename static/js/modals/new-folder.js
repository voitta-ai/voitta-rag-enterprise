// "New folder" modal — two modes behind one dialog.
//
// Root mode (the "All folders" view): creates a managed folder under the
// configured ``VOITTA_ROOT_PATH``; if that env var isn't set, the button
// is gated and we surface an alert instead of opening the modal (folders
// can't materialise without a root). The folder list refresh is implicit —
// the backend emits ``folder.added`` over the WS as soon as the row is
// committed.
//
// Subfolder mode (inside an open folder): creates a directory at the
// current location via mkdir. This replaced the toolbar's separate "New
// subfolder" button (and its prompt()) — the standard flow the usability
// feedback asked for is "New folder" from inside the folder itself.

import { api } from "../api.js";
import { addGhostDir, expand } from "../flows/selection.js";
import { renderFoldersFiltered } from "../render/tree.js";

const $ = (sel) => document.querySelector(sel);

// Server-reported root configuration (path, configured flag). Stashed
// here by ``setRootInfo`` once the bootstrap fetch completes.
let rootInfo = { configured: false, root_path: null };

export function setRootInfo(info) {
    rootInfo = info;
}

// null → root mode; {folderId, relDir} → subfolder mode.
let _subTarget = null;

function show() {
    $("#modal-root-hint").hidden = _subTarget !== null;
    $("#modal-parent-hint").hidden = _subTarget === null;
    $("#modal-backdrop").hidden = false;
    $("#managed-name").value = "";
    $("#managed-name").focus();
}

export function openModal() {
    if (!rootInfo.configured) {
        alert("Set VOITTA_ROOT_PATH in .env to create new folders.");
        return;
    }
    _subTarget = null;
    $("#modal-title").textContent = "New folder";
    $("#modal-root").textContent = rootInfo.root_path;
    show();
}

export function openSubfolderModal(folder, relDir) {
    _subTarget = { folderId: folder.id, relDir };
    $("#modal-title").textContent = "New folder";
    $("#modal-parent").textContent = relDir
        ? `${folder.display_name}/${relDir}`
        : folder.display_name;
    show();
}

export function closeModal() {
    $("#modal-backdrop").hidden = true;
    _subTarget = null;
}

async function create() {
    const name = $("#managed-name").value.trim();
    if (!name) return;
    try {
        if (_subTarget === null) {
            await api.addFolderByName(name);
        } else {
            const { folderId, relDir } = _subTarget;
            const target = relDir ? `${relDir}/${name}` : name;
            await api.mkdir(folderId, target);
            addGhostDir(folderId, target);
            expand(folderId, relDir);
            renderFoldersFiltered();
        }
        closeModal();
    } catch (err) { alert(err.message); }
}

$("#managed-create").addEventListener("click", create);
$("#managed-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") create();
});
