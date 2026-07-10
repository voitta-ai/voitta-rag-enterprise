// ---------------------------------------------------------------------------
// Google Drive LOCAL picker (desktop, no credentials).
//
// Browse the (free, not-downloaded) Drive stub tree one level at a time and
// register a chosen subtree as an indexed-in-place folder via
// POST /sync/local/connect. Read-only: nothing is written back to the Drive.
//
// This is the "This Mac" tab under the google_drive dropdown entry —
// tab == "google_drive", but it registers its own source_type
// (google_drive_local) so saved rows reopen onto this tab.
// ---------------------------------------------------------------------------

import { api } from "../../api.js";
import { closeSyncModal } from "./core.js";
import { getGdAuthMode, setGdAuthMode } from "./google_drive.js";
import { registerSource } from "./registry.js";
import { $, ctx } from "./state.js";

let gdlAccounts = [];          // [{email, path, provider}]
let gdlAccountRoot = "";       // mount root for the selected account
// Canonical set of chosen absolute paths — never two where one is an ancestor
// of the other (the ancestor wins; descendants are pruned). Indexing is
// recursive, so a checked parent already covers its whole subtree.
const gdlSelected = new Set();
const gdlChildrenCache = new Map();  // path -> [entries] (dirs+files)
// Saved subtrees to re-check after the tree builds (set when reopening the
// dialog for an already-connected folder; consumed once by gdlBuildTree).
let gdlPendingRestore = [];

async function gdlRefreshAvailability() {
    // Show the "This Mac (no setup)" Google Drive tab only when the desktop
    // Drive app is running with at least one signed-in account (i.e. on the
    // macOS desktop app). On the server it stays hidden.
    const tab = $("#sync-gd-tab-local");
    if (!tab) return;
    try {
        const res = await api.gdlAccounts();
        tab.hidden = !(res.available && (res.accounts || []).length);
    } catch {
        tab.hidden = true;
    }
}

export async function gdlInit() {
    const sel = $("#sync-gdl-account");
    setGdlStatus("");
    try {
        const res = await api.gdlAccounts();
        gdlAccounts = res.accounts || [];
    } catch {
        gdlAccounts = [];
    }
    sel.innerHTML = "";
    if (!gdlAccounts.length) {
        setGdlStatus("Google Drive isn't available (app not running or no account signed in).", true);
        $("#sync-gdl-list").innerHTML = "";
        $("#sync-gdl-breadcrumb").textContent = "—";
        return;
    }
    for (const a of gdlAccounts) {
        const opt = document.createElement("option");
        opt.value = a.path;
        opt.textContent = a.email;
        sel.appendChild(opt);
    }
    gdlAccountRoot = gdlAccounts[0].path;
    // When restoring a saved selection, land on the account that owns it —
    // the first account is wrong for multi-account Drives.
    if (gdlPendingRestore.length) {
        const first = gdlPendingRestore[0];
        const owner = gdlAccounts.find(
            (a) => first === a.path || first.startsWith(a.path + "/")
        );
        if (owner) {
            gdlAccountRoot = owner.path;
            sel.value = owner.path;
        }
    }
    await gdlBuildTree();
}

function setGdlStatus(msg, isError) {
    const el = $("#sync-gdl-status");
    if (!el) return;
    el.hidden = !msg;
    el.textContent = msg || "";
    el.style.color = isError ? "var(--danger, #c00)" : "";
}

// ---- 3-state selection model (ancestor-wins, like the NFS picker) ----------

function gdlIsAncestorOrSelf(ancestor, candidate) {
    return candidate === ancestor || candidate.startsWith(ancestor + "/");
}
function gdlIsCovered(path) {
    for (const sel of gdlSelected) if (gdlIsAncestorOrSelf(sel, path)) return true;
    return false;
}
function gdlHasSelectedDescendant(path) {
    for (const sel of gdlSelected) {
        if (sel !== path && gdlIsAncestorOrSelf(path, sel)) return true;
    }
    return false;
}
function gdlNodeState(path) {
    if (gdlIsCovered(path)) return "checked";
    if (gdlHasSelectedDescendant(path)) return "indeterminate";
    return "unchecked";
}
function gdlApplyCheckboxState(cb, state) {
    cb.checked = state === "checked";
    cb.indeterminate = state === "indeterminate";
}

async function gdlFetchChildren(path) {
    if (gdlChildrenCache.has(path)) return gdlChildrenCache.get(path);
    const out = await api.gdlBrowse(path);
    const entries = out.entries || [];
    gdlChildrenCache.set(path, entries);
    return entries;
}

function gdlSelect(path) {
    if (gdlIsCovered(path)) return;  // already covered by self/ancestor
    // This subtree subsumes any selected descendants — prune them.
    for (const sel of [...gdlSelected]) {
        if (gdlIsAncestorOrSelf(path, sel)) gdlSelected.delete(sel);
    }
    gdlSelected.add(path);
}

function gdlDeselect(path) {
    if (gdlSelected.has(path)) { gdlSelected.delete(path); return; }
    // Otherwise an ancestor covers it — split that ancestor into the siblings
    // along the chain down to ``path`` (re-selecting everything except the
    // branch we're removing), mirroring the NFS picker's behaviour.
    let covering = "";
    for (const sel of gdlSelected) {
        if (gdlIsAncestorOrSelf(sel, path)) { covering = sel; break; }
    }
    if (!covering) return;
    gdlSelected.delete(covering);
    gdlExpandCoverage(covering, path).catch(() => {});
}

async function gdlExpandCoverage(coveringPath, removePath) {
    let current = coveringPath;
    const tail = removePath.slice(coveringPath.length).replace(/^\/+/, "");
    for (const seg of tail.split("/")) {
        const next = `${current}/${seg}`;
        const children = await gdlFetchChildren(current);
        for (const child of children) {
            if (child.is_dir && child.path !== next && !gdlIsCovered(child.path)) {
                gdlSelected.add(child.path);
            }
        }
        current = next;
    }
    gdlRefreshTreeUi();
}

// Re-apply every visible checkbox's tri-state + refresh the chosen-count line
// and the connect button. Cheap: only the currently-rendered nodes.
function gdlRefreshTreeUi() {
    for (const cb of $("#sync-gdl-list").querySelectorAll(".gdl-cb")) {
        const li = cb.closest("li[data-gdl-path]");
        if (li) gdlApplyCheckboxState(cb, gdlNodeState(li.dataset.gdlPath));
    }
    const n = gdlSelected.size;
    $("#sync-gdl-breadcrumb").textContent =
        n === 0 ? "No folders chosen" : `${n} folder${n === 1 ? "" : "s"} chosen`;
    $("#sync-gdl-connect").disabled = n === 0;
}

function gdlClearSelection() {
    gdlSelected.clear();
    gdlChildrenCache.clear();
    $("#sync-gdl-breadcrumb").textContent = "No folders chosen";
    $("#sync-gdl-connect").disabled = true;
}

// Build the expandable tree: top-level folders of the account mount become
// root nodes; each node lazy-loads its subfolders on expand (so a huge Drive
// never pre-fetches). Browsing is free — listing never downloads file content.
async function gdlBuildTree() {
    gdlClearSelection();
    const list = $("#sync-gdl-list");
    $("#sync-gdl-count").textContent =
        "Click ▶ to expand · tick a folder to index it (incl. its subfolders)";
    list.innerHTML = "<div class='hint' style='padding:6px;'>Loading…</div>";
    let res;
    try {
        res = await api.gdlBrowse(gdlAccountRoot);
        gdlChildrenCache.set(gdlAccountRoot, res.entries || []);
    } catch (e) {
        list.innerHTML = "";
        setGdlStatus(`Couldn't open Drive: ${e.message || e}`, true);
        return;
    }
    setGdlStatus("");
    list.innerHTML = "";
    const ul = document.createElement("ul");
    ul.style.cssText = "list-style:none;margin:0;padding:0;";
    const dirs = res.entries.filter((e) => e.is_dir);
    if (!dirs.length) {
        list.innerHTML = "<div class='hint' style='padding:6px;'>No folders here.</div>";
        return;
    }
    for (const d of dirs) ul.append(gdlBuildNode(d.path, d.name, 0));
    list.append(ul);

    // Reopening an already-connected folder: re-check the saved subtrees and
    // expand down to them so the dialog shows the real state (checked nodes,
    // indeterminate ancestors) instead of a blank tree.
    if (gdlPendingRestore.length) {
        const saved = gdlPendingRestore.filter(
            (p) => p === gdlAccountRoot || p.startsWith(gdlAccountRoot + "/")
        );
        gdlPendingRestore = [];
        await gdlRestoreSelection(saved);
    }
}

// Seed the tri-state model from saved paths and expand each one's ancestor
// chain (lazy-loading as we go) so the restored selection is visible.
async function gdlRestoreSelection(paths) {
    if (!paths.length) return;
    for (const p of paths) gdlSelect(p);
    for (const p of paths) {
        const rel = p.slice(gdlAccountRoot.length).replace(/^\/+/, "");
        let current = gdlAccountRoot;
        for (const seg of rel.split("/").slice(0, -1)) {
            current = `${current}/${seg}`;
            const li = document.querySelector(
                `#sync-gdl-list li[data-gdl-path="${CSS.escape(current)}"]`
            );
            if (!li || !li.gdlExpand) break;  // ancestor gone from the Drive
            try { await li.gdlExpand(); } catch { break; }
        }
    }
    gdlRefreshTreeUi();
}

// Summarise a folder's files by type, e.g. "12 files — 5 pdf · 4 xlsx · 3 docx".
// Native Google docs (.gdoc/.gsheet/.gslides) are labelled as such since they
// index as links (shared ones also export). Shows the top types, then "+N more".
function gdlFileSummary(files) {
    const GDOC = { gdoc: "google doc", gsheet: "google sheet", gslides: "google slides", gdraw: "google drawing", gform: "google form" };
    const counts = {};
    for (const f of files) {
        const dot = f.name.lastIndexOf(".");
        let ext = dot > 0 ? f.name.slice(dot + 1).toLowerCase() : "(no ext)";
        if (f.is_native_doc && GDOC[ext]) ext = GDOC[ext];
        counts[ext] = (counts[ext] || 0) + 1;
    }
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const TOP = 6;
    const shown = sorted.slice(0, TOP).map(([ext, n]) => `${n} ${ext}`);
    const more = sorted.length - TOP;
    if (more > 0) shown.push(`+${more} more`);
    const total = files.length;
    return `${total} file${total === 1 ? "" : "s"} — ${shown.join(" · ")}`;
}

// One folder row: [▶ toggle][☑ checkbox][📁 label] + a hidden child <ul>. The
// chevron expands/collapses (lazy-loading subfolders the first time); the
// checkbox (de)selects that subtree with ancestor-wins tri-state.
function gdlBuildNode(path, name, level) {
    const li = document.createElement("li");
    li.dataset.gdlPath = path;
    li.style.cssText = "list-style:none;padding:0;";

    const row = document.createElement("div");
    row.style.cssText =
        "display:flex;align-items:center;padding:3px 4px 3px " +
        (4 + level * 16) + "px;";

    const toggle = document.createElement("span");
    toggle.textContent = "▶";
    toggle.style.cssText =
        "cursor:pointer;width:14px;display:inline-block;user-select:none;color:var(--text-muted,#888);";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "gdl-cb";
    cb.style.margin = "0 6px";
    gdlApplyCheckboxState(cb, gdlNodeState(path));

    const label = document.createElement("span");
    label.textContent = `📁  ${name}`;
    label.title = name;
    label.style.cssText = "cursor:pointer;flex:1;";

    row.append(toggle, cb, label);
    li.append(row);

    const childUl = document.createElement("ul");
    childUl.style.cssText = "list-style:none;margin:0;padding:0;";
    childUl.hidden = true;
    li.append(childUl);

    let loaded = null;
    async function ensureLoaded() {
        if (loaded) return loaded;
        loaded = (async () => {
            let entries;
            try {
                entries = await gdlFetchChildren(path);
            } catch (e) {
                const err = document.createElement("li");
                err.style.cssText = "padding:3px 0 3px " + (4 + (level + 1) * 16) + "px;color:#dc3545;";
                err.textContent = `error: ${e.message || e}`;
                childUl.append(err);
                return;
            }
            const dirs = entries.filter((e) => e.is_dir);
            const files = entries.filter((e) => !e.is_dir);
            const indent = 4 + (level + 1) * 16;
            // File-type breakdown (shown whenever the folder has files, even
            // alongside subfolders) — what gets indexed if you tick this folder.
            if (files.length) {
                const stats = document.createElement("li");
                stats.className = "hint";
                stats.style.cssText = `padding:3px 0 3px ${indent}px;`;
                stats.textContent = `📄 ${gdlFileSummary(files)}`;
                childUl.append(stats);
            }
            if (!dirs.length && !files.length) {
                const empty = document.createElement("li");
                empty.className = "hint";
                empty.style.cssText = `padding:3px 0 3px ${indent}px;`;
                empty.textContent = "(empty)";
                childUl.append(empty);
            }
            for (const d of dirs) childUl.append(gdlBuildNode(d.path, d.name, level + 1));
        })();
        return loaded;
    }
    async function expand() {
        toggle.textContent = "…";
        await ensureLoaded();
        childUl.hidden = false;
        toggle.textContent = "▼";
        gdlRefreshTreeUi();  // newly-rendered children pick up covered state
    }
    function collapse() {
        childUl.hidden = true;
        toggle.textContent = "▶";
    }
    li.gdlExpand = expand;  // for gdlRestoreSelection — awaits the lazy load
    toggle.addEventListener("click", () => {
        if (childUl.hidden) expand(); else collapse();
    });
    label.addEventListener("click", () => {
        if (childUl.hidden) expand(); else collapse();
    });
    cb.addEventListener("change", () => {
        // Decide by the model's current state, not the checkbox's post-click
        // value: a click on an indeterminate box should SELECT the subtree.
        if (gdlNodeState(path) === "checked") gdlDeselect(path);
        else gdlSelect(path);
        gdlRefreshTreeUi();
    });
    return li;
}

// Google Drive LOCAL: re-root the tree when the account changes.
$("#sync-gdl-account").addEventListener("change", () => {
    gdlAccountRoot = $("#sync-gdl-account").value;
    gdlBuildTree();
});

// Configure the OPENED folder to mirror the checked Drive folders. ONE call:
// the folder's path becomes the account mount, the checked subtrees are
// recorded, and the Drive structure (incl. parent dirs) renders as a tree
// under this folder after sync.
$("#sync-gdl-connect").addEventListener("click", async () => {
    const paths = [...gdlSelected];
    if (!paths.length || !ctx.folderId) return;
    const sel = $("#sync-gdl-account");
    const account = sel.options[sel.selectedIndex]?.textContent || "";  // email
    const btn = $("#sync-gdl-connect");
    btn.disabled = true;
    setGdlStatus(`Connecting ${paths.length} folder${paths.length === 1 ? "" : "s"}…`);
    try {
        await api.gdlConnect({
            folder_id: ctx.folderId,
            account,
            account_root: gdlAccountRoot,
            paths,
            auto_sync_enabled: $("#sync-auto-enabled").checked,
            auto_sync_hours: parseInt($("#sync-auto-hours").value, 10) || 6,
        });
        setGdlStatus("");
        closeSyncModal();
        alert(`Connected ${paths.length} Google Drive folder${paths.length === 1 ? "" : "s"}. They'll appear as a tree under this folder as files index in the background — watch the Recent jobs panel.`);
    } catch (err) {
        setGdlStatus(err.message || String(err), true);
        btn.disabled = false;
    }
});

// The local "This Mac" Google Drive tab CREATES a new indexed folder via its
// own "Connect & index" button, so the shared Drive-folder selector and the
// standard Save / Sync-now / Remove footer + auto-sync row don't apply to it.
// Active iff source == google_drive AND its sub-tab == local.
function gdlHidesChrome() {
    return $("#sync-type").value === "google_drive" && getGdAuthMode() === "local";
}

function loadGdlForm(src) {
    // (core already mapped the dropdown to the google_drive tab via
    // handler.tab, so the form isn't empty when this row reopens.)
    $("#sync-gd-tab-local").hidden = false;
    // Stash the saved subtrees BEFORE switching to the local tab —
    // that triggers gdlInit → gdlBuildTree, which consumes this to
    // pre-check the tree with the current selection.
    gdlPendingRestore = src.google_drive_local?.paths || [];
    setGdAuthMode("local");
    const status = $("#sync-gdl-status");
    if (status && src.google_drive_local) {
        status.hidden = false;
        status.textContent =
            `Indexing ${src.google_drive_local.path} ` +
            `(${src.google_drive_local.status}). Use "Sync now" from the folder menu to refresh.`;
    }
    return true;  // no shared footer / auto-sync tail for the local tab
}

registerSource({
    type: "google_drive_local",
    tab: "google_drive",
    reset: gdlRefreshAvailability,
    load: loadGdlForm,
    hidesChrome: gdlHidesChrome,
});
