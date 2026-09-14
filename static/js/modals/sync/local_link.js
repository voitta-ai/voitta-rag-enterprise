// ---------------------------------------------------------------------------
// Linked folder — a host directory indexed IN PLACE.
//
// Nothing is copied: the connect call re-points the folder at the chosen
// directory and the server's rescan walks it where it is. The picker is a
// single-select directory browser under the admin's linked-folder root, one
// level at a time. One directory per folder — a linked folder IS a directory,
// so multi-select has no meaning here.
//
// Lifecycle mirrors google_drive_local: no formConfig, and the shared
// Save / Sync-now / Remove footer is hidden (hidesChrome) because the server
// side is its own connect endpoint rather than the PUT envelope. Unlike the
// Drive tab it KEEPS the auto-sync row — that is the rescan schedule.
// ---------------------------------------------------------------------------

import { api } from "../../api.js";
import { closeSyncModal, renderSyncStatus } from "./core.js";
import { registerSource } from "./registry.js";
import { $, ctx } from "./state.js";

let linkAvailable = false;
let linkRoot = "";
let linkCwd = "";              // rel_path of the directory currently listed
let linkSelected = null;       // rel_path chosen with "Use this directory"
let linkSelectedAbs = "";      // its absolute path, for display

async function linkRefreshStatus() {
    const opt = $("#sync-type-option-local_link");
    const rootDisplay = $("#sync-link-root-display");
    try {
        const s = await api.linkStatus();
        linkAvailable = !!s.available;
        linkRoot = s.link_root || "";
        if (opt) opt.hidden = !s.available;
        if (rootDisplay) {
            rootDisplay.value = linkRoot;
            rootDisplay.placeholder = s.available
                ? ""
                : linkRoot
                    ? `${linkRoot} — unavailable (${s.status})`
                    : "(set the linked-folder root in Admin → Storage)";
        }
        return s;
    } catch {
        linkAvailable = false;
        if (opt) opt.hidden = true;
        return { available: false, status: "error", link_root: "" };
    }
}

function linkSetStatus(msg, isError = false) {
    const el = $("#sync-link-status");
    if (!el) return;
    el.hidden = !msg;
    el.textContent = msg || "";
    el.style.color = isError ? "#dc3545" : "";
}

function linkRenderSelection() {
    const el = $("#sync-link-selected");
    const btn = $("#sync-link-connect");
    if (linkSelected) {
        el.textContent = `Selected: ${linkSelectedAbs}`;
        btn.disabled = false;
    } else {
        el.textContent = "Nothing selected.";
        btn.disabled = true;
    }
}

// Serialise browses: setSyncType fires one (onShow) and loadSyncSource fires
// another with the saved path a few ms later. Chaining guarantees the later
// call's render lands last.
let linkBrowseChain = Promise.resolve();

function linkBrowse(rel) {
    linkBrowseChain = linkBrowseChain.catch(() => {}).then(() => _linkBrowseImpl(rel));
    return linkBrowseChain;
}

async function _linkBrowseImpl(rel) {
    const list = $("#sync-link-list");
    const crumb = $("#sync-link-breadcrumb");
    const useBtn = $("#sync-link-use");
    const upBtn = $("#sync-link-up");
    list.innerHTML = "";
    if (!linkAvailable) {
        const li = document.createElement("li");
        li.className = "muted";
        li.style.padding = "8px";
        li.textContent = "Linked folders are unavailable — ask an admin to set the linked-folder root.";
        list.append(li);
        useBtn.disabled = true;
        upBtn.disabled = true;
        return;
    }
    let out;
    try {
        out = await api.linkBrowse(rel);
    } catch (err) {
        const li = document.createElement("li");
        li.style.padding = "8px";
        li.style.color = "#dc3545";
        li.textContent = `error: ${err.message}`;
        list.append(li);
        return;
    }
    linkCwd = out.rel_path || "";
    crumb.textContent = out.path || linkRoot;
    crumb.title = crumb.textContent;
    upBtn.disabled = out.parent === null || out.parent === undefined;
    useBtn.disabled = !linkCwd;  // the root itself cannot be linked
    useBtn.dataset.rel = linkCwd;
    useBtn.dataset.abs = out.path || "";
    const entries = out.entries || [];
    if (!entries.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.style.padding = "4px 8px";
        li.textContent = "(no subdirectories)";
        list.append(li);
    }
    for (const e of entries) {
        const li = document.createElement("li");
        li.style.padding = "3px 8px";
        li.style.cursor = "pointer";
        li.textContent = `▸ ${e.name}`;
        li.title = e.rel_path;
        li.addEventListener("click", () => linkBrowse(e.rel_path));
        list.append(li);
    }
}

$("#sync-link-up").addEventListener("click", () => {
    const parts = linkCwd.split("/").filter(Boolean);
    parts.pop();
    linkBrowse(parts.join("/"));
});

$("#sync-link-use").addEventListener("click", () => {
    const b = $("#sync-link-use");
    if (!b.dataset.rel) return;
    linkSelected = b.dataset.rel;
    linkSelectedAbs = b.dataset.abs;
    linkRenderSelection();
});

function linkIgnoreList() {
    return $("#sync-link-ignore").value
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean);
}

$("#sync-link-connect").addEventListener("click", async () => {
    if (!linkSelected || !ctx.folderId) return;
    const btn = $("#sync-link-connect");
    btn.disabled = true;
    linkSetStatus(`Linking ${linkSelectedAbs}…`);
    try {
        await api.linkConnect({
            folder_id: ctx.folderId,
            rel_path: linkSelected,
            ignore: linkIgnoreList(),
            auto_sync_enabled: $("#sync-auto-enabled").checked,
            auto_sync_hours: parseInt($("#sync-auto-hours").value, 10) || 1,
        });
        linkSetStatus("");
        closeSyncModal();
        alert(`Linked ${linkSelectedAbs}. Files are indexed where they are — nothing was copied. Watch the Recent jobs panel for progress.`);
    } catch (err) {
        linkSetStatus(err.message || String(err), true);
        btn.disabled = false;
    }
});

$("#sync-link-rescan").addEventListener("click", async () => {
    if (!ctx.folderId) return;
    try {
        await api.triggerSync(ctx.folderId);
        closeSyncModal();
        alert("Rescan queued. Watch the Recent jobs panel for progress.");
    } catch (err) {
        linkSetStatus(err.message || String(err), true);
    }
});

function linkReset() {
    linkSelected = null;
    linkSelectedAbs = "";
    linkCwd = "";
    const ta = $("#sync-link-ignore");
    if (ta) ta.value = "";
    $("#sync-link-rescan").hidden = true;
    linkSetStatus("");
    linkRenderSelection();
    return linkRefreshStatus();
}

function linkOnShow() {
    linkRefreshStatus().then(() => linkBrowse(linkCwd));
}

async function loadLinkForm(src) {
    if (!src.local_link) return;
    await linkRefreshStatus();
    linkSelected = src.local_link.rel_path || null;
    linkSelectedAbs = src.local_link.path || "";
    $("#sync-link-ignore").value = (src.local_link.ignore || []).join("\n");
    $("#sync-auto-enabled").checked = !!src.auto_sync_enabled;
    const hrs = Math.max(1, Math.min(24, Number(src.auto_sync_hours) || 1));
    $("#sync-auto-hours").value = String(hrs);
    $("#sync-auto-hours").disabled = !src.auto_sync_enabled;
    $("#sync-link-rescan").hidden = false;
    linkRenderSelection();
    renderSyncStatus(src);
    if (!src.local_link.available) {
        linkSetStatus(`Linked directory is ${src.local_link.status}: ${src.local_link.path}`, true);
    }
    await linkBrowse(linkSelected || "");
    return true;  // own footer — the shared Save / Sync-now / Remove tail doesn't apply
}

registerSource({
    type: "local_link",
    tab: "local_link",
    paneId: "#sync-form-local_link",
    reset: linkReset,
    onShow: linkOnShow,
    load: loadLinkForm,
    hidesChrome: () => $("#sync-type").value === "local_link",
    keepsAutoSync: () => true,
});
