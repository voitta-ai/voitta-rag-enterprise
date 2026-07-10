// Google Drive sync section + Google Drive folder picker.
//
// One Drive sync source per managed folder root. The modal loads the
// existing config (if any), lets the user edit + save, and offers
// "Sync now" to enqueue a sync job.
//
// Two auth modes:
// - OAuth: client_id + client_secret + Connect → refresh_token. The
//   provider picker fills the credential fields from saved
//   ``/api/admin/auth-providers`` rows; manual entry still works.
// - Service account: paste a key JSON. Folder picker can list folders
//   the SA has been granted access to.
//
// The folder picker submodal is co-located here because it's tightly
// coupled — it reads the in-memory ``gdFolders`` list, writes the
// user's selection back via ``setGdFolders``, and is only opened
// from this file.
//
// (The third tab — the desktop-only "This Mac" local Drive picker —
// lives in google_local.js; ``setGdAuthMode("local")`` hands off to it.
// The two modules import each other's functions, which is a benign
// cycle: only hoisted function bindings are referenced, at call time.)

import { api } from "../../api.js";
import { me } from "../../store.js";
import { applyChrome, loadSyncSource, syncBody } from "./core.js";
import { gdlInit } from "./google_local.js";
import { registerSource } from "./registry.js";
import { $, ctx } from "./state.js";

// In-memory cache of the Google providers fetched from
// /api/admin/auth-providers. Populated when the GD sync modal opens
// (refreshGdProviderPicker) and consulted whenever the picker fires
// onchange — keeps the populate path synchronous so users see the
// fields fill the instant they pick a row, with no flicker.
//
// Map<id, {id, label, client_id, client_secret, source}>.
const gdGoogleProviders = new Map();

async function refreshGdProviderPicker() {
    const row = $("#sync-gd-provider-row");
    const sel = $("#sync-gd-provider-picker");
    sel.innerHTML = '<option value="">— Manual entry —</option>';
    gdGoogleProviders.clear();

    let providers;
    try {
        providers = await api.adminListAuthProviders();
    } catch (err) {
        // The list is readable by any authenticated user, so a failure
        // here is unexpected (network / not signed in). Log it and fall
        // back to the manual-entry flow with the picker hidden.
        if (!String(err.message || "").startsWith("403")) {
            console.warn("provider list failed", err);
        }
        row.hidden = true;
        return;
    }

    const enabledGoogle = (providers || []).filter(
        (p) => p.provider === "google" && p.enabled,
    );
    if (!enabledGoogle.length) {
        row.hidden = true;
        return;
    }

    for (const p of enabledGoogle) {
        gdGoogleProviders.set(p.id, p);
        const opt = document.createElement("option");
        opt.value = String(p.id);
        const tail = p.source === "env" ? "  (.env)" : "";
        opt.textContent = (p.label || p.client_id) + tail;
        sel.append(opt);
    }
    row.hidden = false;
}

function applyGdProviderSelection(providerId) {
    const pane = $("#sync-gd-pane-oauth");
    const idInput = $("#sync-gd-client-id");
    const secretInput = $("#sync-gd-client-secret");
    if (!providerId) {
        pane.classList.remove("provider-picked");
        // Manual entry — leave whatever the user has typed alone.
        return;
    }
    const p = gdGoogleProviders.get(Number(providerId));
    if (!p) return;
    idInput.value = p.client_id || "";
    secretInput.value = p.client_secret || "";
    // ``input`` events drive setGdConnState so the Connect button can
    // unlock; fire them so picking a provider feels identical to typing
    // the values manually.
    idInput.dispatchEvent(new Event("input", { bubbles: true }));
    secretInput.dispatchEvent(new Event("input", { bubbles: true }));
    pane.classList.add("provider-picked");
}

function preselectGdProviderByClientId(clientId) {
    const sel = $("#sync-gd-provider-picker");
    if (!clientId) { sel.value = ""; return; }
    for (const p of gdGoogleProviders.values()) {
        if (p.client_id === clientId) {
            sel.value = String(p.id);
            $("#sync-gd-pane-oauth").classList.add("provider-picked");
            return;
        }
    }
    sel.value = ""; // existing config doesn't match a saved provider
}

// Fixed-port loopback redirect — kept in lockstep with the backend's
// GD_LOOPBACK_REDIRECT_URI. Admins paste this verbatim into Google
// Cloud Console; a local nginx bridge listens on 53682 and proxies the
// callback back to this server.
const GD_LOOPBACK_REDIRECT_URI =
    "http://localhost:53682/api/sync/oauth/google/callback";

function updateGdRedirectHint() {
    const useLoopback = $("#sync-gd-use-loopback")?.checked;
    const hint = $("#sync-gd-redirect-hint");
    if (hint) {
        hint.textContent = useLoopback
            ? GD_LOOPBACK_REDIRECT_URI
            : `${window.location.origin}/api/sync/oauth/google/callback`;
    }
    const loopbackHint = $("#sync-gd-loopback-hint");
    if (loopbackHint) loopbackHint.hidden = !useLoopback;
}

// Mirror of the user's current selection in the GD picker; flushed to the
// form on every set. Keeps the rendered list and the form-config call in
// sync without parsing the DOM at submit time.
let gdFolders = [];

// Last-known-saved set of Drive folder IDs. Used by the Save handler
// to detect folder *removals* (the cleanup pass deletes locally-mirrored
// files for any folder that's no longer selected — see
// services/sync/google_drive.py). When the user removes folders we
// prompt before saving so they don't accidentally trash indexed data
// without realizing it. Refreshed every time the modal loads from
// server, every successful Save, and every Sync-now.
let savedGdFolderIds = new Set();

function _snapshotSavedGdFolders(folders) {
    savedGdFolderIds = new Set((folders || []).map((f) => String(f.id || "")));
}

// Which credential pane is showing — drives both the rendered inputs and
// what gets serialised in gdFormConfig. The two flows have meaningfully
// different gotchas (OAuth needs Connect + redirect URI; SA needs
// folder-shared-with-client_email), so we render exactly one set of
// fields and persist the mode the user actually used.
let gdAuthMode = "oauth"; // "builtin" | "oauth" | "sa" | "local"

export function getGdAuthMode() {
    return gdAuthMode;
}

export function setGdAuthMode(mode) {
    gdAuthMode = ["builtin", "sa", "local"].includes(mode) ? mode : "oauth";
    const builtinTab = $("#sync-gd-tab-builtin");
    const oauthTab = $("#sync-gd-tab-oauth");
    const saTab = $("#sync-gd-tab-sa");
    const localTab = $("#sync-gd-tab-local");
    for (const [tab, m] of [[builtinTab, "builtin"], [oauthTab, "oauth"], [saTab, "sa"], [localTab, "local"]]) {
        if (!tab) continue;
        tab.classList.toggle("active", gdAuthMode === m);
        tab.setAttribute("aria-selected", gdAuthMode === m ? "true" : "false");
    }
    $("#sync-gd-pane-builtin").hidden = gdAuthMode !== "builtin";
    $("#sync-gd-pane-oauth").hidden = gdAuthMode !== "oauth";
    $("#sync-gd-pane-sa").hidden = gdAuthMode !== "sa";
    $("#sync-gd-pane-local").hidden = gdAuthMode !== "local";

    // The local "This Mac" tab is a CREATE flow with its own button — hide the
    // shared selector + standard footer, and (re)load the folder picker.
    applyChrome();
    if (gdAuthMode === "local") {
        gdlInit();
        return;  // none of the OAuth/SA folder-selector wiring applies
    }

    // Pick browser only works in OAuth mode (it needs gd_refresh_token).
    // In SA mode, surface the typed-folder-id input as the only path and
    // keep the Pick button visible-but-disabled with a clear hint.
    const pickBtn = $("#sync-gd-pick-folder");
    const idInput = $("#sync-gd-add-folder-id");
    if (gdAuthMode === "builtin") {
        // Built-in client: same connected-gated Pick/Test as OAuth, but the
        // connection state lives on the builtin pane's own status span.
        const connected = $("#sync-gd-conn-status-builtin").textContent.startsWith("Connected");
        pickBtn.hidden = false;
        pickBtn.disabled = !connected;
        pickBtn.title = connected ? "Pick a Drive folder" : "Connect first";
        const testBtn = $("#sync-gd-test-apis");
        testBtn.disabled = !connected;
        testBtn.title = connected ? "Probe which Workspace APIs are enabled" : "Connect first";
        idInput.placeholder = "Or paste a folder ID and press Enter";
        $("#sync-gd-folders-hint").textContent =
            "Each picked folder syncs into its own subdirectory under this folder.";
    } else if (gdAuthMode === "sa") {
        // Pick works in SA mode too — the backend mints an SA access token
        // and lists everything shared with the SA's client_email plus any
        // Shared Drives it's a member of. We still need a saved SA JSON
        // before the API call will succeed.
        const hasSaSaved = $("#sync-gd-sa-json").placeholder.startsWith(
            "(service account JSON saved",
        );
        const hasSaTyped = $("#sync-gd-sa-json").value.trim().length > 0;
        const saReady = hasSaSaved || hasSaTyped;
        pickBtn.hidden = false;
        pickBtn.disabled = !saReady;
        pickBtn.title = saReady
            ? "Pick a folder shared with the service account"
            : "Save a service-account JSON first";
        const testBtn = $("#sync-gd-test-apis");
        testBtn.disabled = !saReady;
        testBtn.title = saReady ? "Probe which Workspace APIs are enabled" : "Save a service-account JSON first";
        idInput.placeholder = "Or paste a folder ID and press Enter";
        $("#sync-gd-folders-hint").innerHTML =
            "Pick from folders shared with the service account, or paste a Drive " +
            "folder ID directly. <strong>The service account only sees folders explicitly " +
            "shared with its <code>client_email</code></strong> (Viewer is enough). Each " +
            "folder syncs into its own subdirectory under this folder.";
    } else {
        pickBtn.hidden = false;
        idInput.placeholder = "Or paste a folder ID and press Enter";
        // The OAuth pane controls Pick-button enablement via setGdConnState
        // — call it with whatever connection state we currently know.
        const connected = $("#sync-gd-conn-status").textContent.startsWith("Connected");
        const hasClientSecret = $("#sync-gd-client-secret").placeholder.startsWith("(saved");
        setGdConnState({ connected, hasClientSecret });
        $("#sync-gd-folders-hint").textContent =
            "Each picked folder syncs into its own subdirectory under this folder.";
    }
}

function setGdFolders(folders) {
    gdFolders = (folders || []).map((f) => ({ id: String(f.id || ""), name: String(f.name || "") }));
    const ul = $("#sync-gd-folders-list");
    ul.innerHTML = "";
    if (gdFolders.length === 0) {
        $("#sync-gd-folder-count").textContent = "none selected";
    } else {
        $("#sync-gd-folder-count").textContent = `${gdFolders.length} folder${gdFolders.length === 1 ? "" : "s"} selected`;
        for (const f of gdFolders) {
            const li = document.createElement("li");
            li.textContent = f.name ? `${f.name}` : f.id;
            ul.append(li);
        }
    }
}

function setGdConnState({ connected, hasClientSecret }) {
    $("#sync-gd-conn-status").textContent = connected ? "Connected ✓" : "Not connected";
    const connectBtn = $("#sync-gd-connect");
    const pickBtn = $("#sync-gd-pick-folder");
    // Connect needs client_id + (saved or just-typed) client_secret.
    const clientId = $("#sync-gd-client-id").value.trim();
    const secretAvailable = hasClientSecret || $("#sync-gd-client-secret").value.trim().length > 0;
    connectBtn.disabled = !(clientId && secretAvailable);
    connectBtn.title = connectBtn.disabled
        ? "Save client_id and client_secret first"
        : (connected ? "Re-connect (forces a fresh consent)" : "Connect");
    // Pick is OAuth-only — leave the SA-mode disabled state alone.
    if (gdAuthMode === "oauth") {
        pickBtn.disabled = !connected;
        pickBtn.title = connected ? "Pick a Drive folder" : "Connect first";
        const testBtn = $("#sync-gd-test-apis");
        testBtn.disabled = !connected;
        testBtn.title = connected ? "Probe which Workspace APIs are enabled" : "Connect first";
    }
}

$("#sync-gd-tab-builtin").addEventListener("click", () => setGdAuthMode("builtin"));
$("#sync-gd-tab-oauth").addEventListener("click", () => setGdAuthMode("oauth"));
$("#sync-gd-tab-sa").addEventListener("click", () => setGdAuthMode("sa"));
$("#sync-gd-tab-local").addEventListener("click", () => setGdAuthMode("local"));

// Manual folder-ID add — Enter accepts, ignores duplicates and empties.
// Folder names aren't known here (no Drive lookup in SA mode); the
// rendered list falls back to showing the bare ID, which is fine.
$("#sync-gd-add-folder-id").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const id = $("#sync-gd-add-folder-id").value.trim();
    if (!id) return;
    if (gdFolders.some((f) => f.id === id)) {
        $("#sync-gd-add-folder-id").value = "";
        return;
    }
    setGdFolders([...gdFolders, { id, name: "" }]);
    $("#sync-gd-add-folder-id").value = "";
});

function gdFormConfig() {
    // Only send credentials for the active mode. The inactive pane's
    // inputs may still hold leftover text the user typed before switching
    // tabs — sending both would let the back end pick whichever path it
    // prefers, which would be confusing. Empty strings are safe: the
    // back-end PUT validator preserves stored secrets when blank values
    // arrive (so "saved" placeholders aren't wiped on every save).
    const useLoopback = !!$("#sync-gd-use-loopback")?.checked;
    const filesOnly = !!$("#sync-gd-files-only")?.checked;
    if (gdAuthMode === "builtin") {
        // Built-in client: no user credentials at all; the loopback
        // bridge doesn't apply (desktop reaches 127.0.0.1 directly).
        return {
            client_id: "",
            client_secret: "",
            folders: gdFolders,
            service_account_json: "",
            use_loopback: false,
            use_builtin: true,
            files_only: filesOnly,
        };
    }
    if (gdAuthMode === "sa") {
        return {
            client_id: "",
            client_secret: "",
            folders: gdFolders,
            service_account_json: $("#sync-gd-sa-json").value,
            use_loopback: useLoopback,
            use_builtin: false,
            files_only: filesOnly,
        };
    }
    return {
        client_id: $("#sync-gd-client-id").value.trim(),
        client_secret: $("#sync-gd-client-secret").value,
        folders: gdFolders,
        service_account_json: "",
        use_loopback: useLoopback,
        use_builtin: false,
        files_only: filesOnly,
    };
}

// Provider picker — populating the credential fields from a saved
// row. Manual edits afterwards are preserved (Save uses the inputs
// verbatim); the accent border on the inputs comes from the
// ``provider-picked`` class set in applyGdProviderSelection.
$("#sync-gd-provider-picker").addEventListener("change", (e) => {
    applyGdProviderSelection(e.target.value);
});

// Re-evaluate Connect-button state when the user types in the GD inputs.
$("#sync-gd-client-id").addEventListener("input", () =>
    setGdConnState({
        connected: $("#sync-gd-conn-status").textContent.startsWith("Connected"),
        hasClientSecret: $("#sync-gd-client-secret").placeholder.startsWith("(saved"),
    }));
$("#sync-gd-client-secret").addEventListener("input", () =>
    setGdConnState({
        connected: $("#sync-gd-conn-status").textContent.startsWith("Connected"),
        hasClientSecret: $("#sync-gd-client-secret").placeholder.startsWith("(saved"),
    }));

// Loopback toggle flips the redirect-hint between the server's origin
// and the fixed http://localhost:53682/... URL the admin should
// register in GCP. Doesn't save by itself — the value is sent with
// the next gdFormConfig() Save.
$("#sync-gd-use-loopback").addEventListener("change", updateGdRedirectHint);

// "Test API availability" — probe which Workspace APIs the OAuth client
// can reach. Drive down = fatal; Docs/Sheets/Slides/Forms down = offer
// files-only. Doesn't save; the result just guides the user before they
// pick folders / save / trigger.
$("#sync-gd-test-apis").addEventListener("click", async () => {
    const btn = $("#sync-gd-test-apis");
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Testing…";
    $("#sync-gd-test-hint").textContent = "";
    try {
        const out = await api.gdApiStatus(ctx.folderId);
        renderGdApiStatus(out);
    } catch (err) {
        const panel = $("#sync-gd-api-result");
        panel.hidden = false;
        panel.innerHTML = "";
        panel.style.borderLeft = "3px solid var(--color-warning)";
        panel.style.padding = "8px 10px";
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = `API test failed: ${err.message}`;
        panel.appendChild(p);
    } finally {
        btn.disabled = false;
        btn.textContent = prev;
    }
});

const _GD_API_ROWS = [
    ["drive", "Google Drive"],
    ["docs", "Google Docs"],
    ["sheets", "Google Sheets"],
    ["slides", "Google Slides"],
    ["forms", "Google Forms"],
];

// Render the GoogleDriveApiStatusOut payload into the result panel: a
// ✓/✗ row per API (with a GCP "enable" link for disabled ones) plus an
// overall verdict. When only the native-export APIs are down we auto-tick
// files-only so the user can save + sync immediately; when Drive itself
// is down we flag it as fatal.
function renderGdApiStatus(out) {
    const panel = $("#sync-gd-api-result");
    panel.hidden = false;
    panel.innerHTML = "";
    panel.style.borderLeft = "3px solid var(--color-warning)";
    panel.style.padding = "8px 10px";

    if (out.scope_problem) {
        const h = document.createElement("strong");
        h.textContent = "OAuth token is missing required scopes.";
        panel.appendChild(h);
        const p = document.createElement("p");
        p.className = "hint";
        p.style.margin = "4px 0 0";
        p.textContent = "Reconnect Google Drive (click Connect) to re-grant access, then test again.";
        panel.appendChild(p);
        return;
    }

    const disabledUrls = new Map((out.disabled || []).map(([label, url]) => [label, url]));
    const ul = document.createElement("ul");
    ul.style.margin = "4px 0";
    ul.style.paddingLeft = "18px";
    for (const [key, label] of _GD_API_ROWS) {
        const li = document.createElement("li");
        const ok = !!out[key];
        li.textContent = `${ok ? "✓" : "✗"} ${label}`;
        li.style.color = ok ? "var(--color-text)" : "var(--color-warning)";
        if (!ok && disabledUrls.has(label)) {
            li.appendChild(document.createTextNode(" — "));
            const a = document.createElement("a");
            a.href = disabledUrls.get(label);
            a.target = "_blank";
            a.rel = "noopener";
            a.textContent = "enable in GCP";
            li.appendChild(a);
        }
        ul.appendChild(li);
    }
    panel.appendChild(ul);

    const msg = document.createElement("p");
    msg.className = "hint";
    msg.style.margin = "4px 0 0";
    if (!out.drive_ok) {
        panel.style.borderLeftColor = "var(--color-danger, #ff3b30)";
        const strong = document.createElement("strong");
        strong.textContent = "The Google Drive API is disabled — sync cannot run.";
        msg.appendChild(strong);
        msg.appendChild(document.createTextNode(
            " Enable it in the GCP console (link above), then test again."));
    } else if (!out.native_ok) {
        const strong = document.createElement("strong");
        strong.textContent = "Some native-export APIs are disabled.";
        msg.appendChild(strong);
        msg.appendChild(document.createTextNode(
            " Sync will automatically skip only those native types (✗ above) " +
            "and still export the ones whose API is enabled (✓), plus all binary " +
            "files (PDF, DOCX, images, …). No action needed — enable the missing " +
            "APIs in GCP if you also want those types indexed."));
    } else {
        msg.appendChild(document.createTextNode(
            "All Workspace APIs are enabled — full sync (including native " +
            "Docs / Sheets / Slides / Forms) will work."));
    }
    panel.appendChild(msg);
}

// Drive-folder removals trigger orphan-cleanup on the next sync —
// files mirrored locally for any removed folder get deleted from disk,
// then the watcher cascades that into SQLite + Qdrant removals. The
// user almost certainly wants that, but they need to *know* it's
// happening — silent data loss is the worst failure mode. We compare
// the current selection against ``savedGdFolderIds`` (snapshot from
// the server's last state) and warn before saving when anything got
// dropped.
function _removedGdFolderCount() {
    const current = new Set(gdFolders.map((f) => String(f.id || "")));
    let removed = 0;
    for (const id of savedGdFolderIds) {
        if (!current.has(id)) removed += 1;
    }
    return removed;
}

// If the user removed Drive folders since the last load/save,
// make sure they understand the cleanup that's coming on the
// next sync. "Save & sync now" is the obvious follow-through;
// "Save only" lets them stage the change for later.
function gdBeforeSaveConfirm() {
    const removed = _removedGdFolderCount();
    if (removed > 0) {
        const msg =
            `You removed ${removed} Drive folder${removed === 1 ? "" : "s"} ` +
            `from this sync.\n\n` +
            `On the next sync, files mirrored from the removed folder(s) ` +
            `will be deleted from disk, then their SQLite rows and Qdrant ` +
            `points will be wiped.\n\n` +
            `Save and trigger that sync NOW?\n\n` +
            `• OK = Save & Sync now (cleanup runs immediately)\n` +
            `• Cancel = Save only (cleanup waits for your next Sync-now ` +
            `or the auto-sync timer)`;
        return confirm(msg);
    }
    return false;
}

// Google Drive: launch the OAuth flow in a popup. The callback closes its
// own tab and the server publishes folder.gd_connected; we re-load on
// modal focus to catch the new state without needing a websocket subscription.
// Shared by the user-supplied-client Connect and the built-in-client Connect
// — the save that precedes gdAuthInit serialises whichever mode is active.
async function gdConnectFlow() {
    try {
        // Save first so the server has the auth config to issue the
        // auth URL with (client creds, or just the use_builtin flag).
        await api.putSync(ctx.folderId, syncBody());
        const { auth_url } = await api.gdAuthInit(ctx.folderId);
        const popup = window.open(auth_url, "voitta-gd-auth", "width=520,height=640");
        if (!popup) {
            alert("Popup blocked. Allow popups for this site and click Connect again.");
            return;
        }
        // Poll until the popup closes, then refresh the source.
        const t = setInterval(async () => {
            if (popup.closed) {
                clearInterval(t);
                await loadSyncSource();
            }
        }, 500);
    } catch (err) {
        alert(err.message);
    }
}
$("#sync-gd-connect").addEventListener("click", gdConnectFlow);
$("#sync-gd-connect-builtin").addEventListener("click", gdConnectFlow);

$("#sync-gd-pick-folder").addEventListener("click", async () => {
    try {
        const data = await api.gdListFolders(ctx.folderId);
        openGdPicker(data);
    } catch (err) {
        alert(err.message);
    }
});

// ---------------------------------------------------------------------------
// Google Drive folder-picker — lazy tree with 3-state checkboxes
// ---------------------------------------------------------------------------
//
// State model
// -----------
// gdPickNodes  map<id, node>  one entry per Drive folder seen so far
// gdPickRoots  array<id>      top-level IDs in display order
//
// Each node: { id, name, driveId, owner_email, shared_at, modified_at,
//              checked: bool, indeterminate: bool,
//              children: null | array<id>,   // null = not yet fetched
//              loading: bool, expanded: bool }
//
// Checkbox tri-state rule
// -----------------------
// - checked=true  indeterminate=false → this folder and everything under it
// - checked=false indeterminate=true  → some children are checked
// - checked=false indeterminate=false → nothing selected in subtree
//
// Propagation: checking a node forces all loaded descendants to the same
// state.  Unchecking / partial-checking a node recalculates ancestors.

let gdPickNodes = new Map();   // id → node
let gdPickRoots = [];          // ordered top-level ids

function _gdNode(f, driveId = "") {
    return {
        id: f.id, name: f.name, driveId,
        owner_email: f.owner_email || "",
        shared_at: f.shared_at || "",
        modified_at: f.modified_at || "",
        checked: false, indeterminate: false,
        children: null, loading: false, expanded: false,
    };
}

function openGdPicker(data) {
    gdPickNodes = new Map();
    gdPickRoots = [];
    const preselected = new Set(gdFolders.map((f) => f.id));

    const groups = [
        { label: "My Drive",       items: data.folders,        driveId: "" },
        { label: "Shared with me", items: data.shared_folders, driveId: "" },
        { label: "Shared drives",  items: data.shared_drives,  driveId: "" },
    ];
    // Shared Drives: the drive id IS the root folder id for browsing.
    for (const g of groups) {
        for (const f of (g.items || [])) {
            const node = _gdNode(f, g.label === "Shared drives" ? f.id : "");
            node.checked = preselected.has(f.id);
            gdPickNodes.set(f.id, node);
            gdPickRoots.push({ id: f.id, group: g.label });
        }
    }

    if (gdPickNodes.size === 0) { alert("No Drive folders found."); return; }

    _gdRenderTree();
    $("#gd-pick-backdrop").hidden = false;
}

function _gdRenderTree() {
    const list = $("#gd-pick-list");
    list.innerHTML = "";

    const groups = ["My Drive", "Shared with me", "Shared drives"];
    for (const group of groups) {
        const rootIds = gdPickRoots.filter((r) => r.group === group).map((r) => r.id);
        if (!rootIds.length) continue;
        const header = document.createElement("div");
        header.className = "gd-tree-group";
        header.textContent = group;
        list.append(header);
        for (const id of rootIds) {
            list.append(_gdBuildRow(id, 0));
        }
    }
    _gdRefreshTopCheckbox();
}

function _gdBuildRow(id, depth) {
    const node = gdPickNodes.get(id);
    const wrap = document.createElement("div");
    wrap.dataset.gdId = id;

    const row = document.createElement("div");
    row.className = "gd-tree-row";
    row.style.paddingLeft = `${depth * 16 + 4}px`;

    // Expander button (▶ / ▼ / spinner / leaf dot)
    const exp = document.createElement("button");
    exp.type = "button";
    exp.className = "gd-tree-exp";
    exp.setAttribute("aria-label", node.expanded ? "Collapse" : "Expand");
    exp.textContent = node.loading ? "⋯" : node.expanded ? "▾" : node.children === null ? "▸" : node.children.length ? "▸" : "·";
    if (node.children !== null && !node.children.length) exp.disabled = true;
    exp.addEventListener("click", () => _gdToggleExpand(id));

    // Checkbox
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "gd-tree-cb";
    cb.checked = node.checked;
    cb.indeterminate = node.indeterminate;
    cb.addEventListener("change", () => _gdToggleCheck(id, cb.checked));

    // Label
    const label = document.createElement("span");
    label.className = "gd-tree-label";
    const title = document.createElement("span");
    title.textContent = node.name;
    label.append(title);
    const sub = _gdSubtitle(node);
    if (sub) {
        const s = document.createElement("span");
        s.className = "gd-tree-sub";
        s.textContent = sub;
        label.append(s);
    }

    row.append(exp, cb, label);
    wrap.append(row);

    if (node.expanded && node.children) {
        const kids = document.createElement("div");
        kids.className = "gd-tree-children";
        for (const cid of node.children) {
            kids.append(_gdBuildRow(cid, depth + 1));
        }
        wrap.append(kids);
    }

    return wrap;
}

async function _gdToggleExpand(id) {
    const node = gdPickNodes.get(id);
    if (node.loading) return;

    if (node.expanded) {
        node.expanded = false;
        _gdReRenderRow(id);
        return;
    }

    if (node.children === null) {
        // Lazy-load children from the server.
        node.loading = true;
        _gdReRenderRow(id);
        try {
            const children = await api.gdBrowseFolder(ctx.folderId, id, node.driveId);
            node.children = children.map((f) => {
                if (!gdPickNodes.has(f.id)) {
                    const child = _gdNode(f, node.driveId);
                    // Inherit checked state from parent if parent is fully checked.
                    if (node.checked) child.checked = true;
                    gdPickNodes.set(f.id, child);
                }
                return f.id;
            });
        } catch (err) {
            node.loading = false;
            _gdReRenderRow(id);
            alert(`Could not load subfolders: ${err.message}`);
            return;
        }
        node.loading = false;
    }

    node.expanded = node.children.length > 0;
    _gdReRenderRow(id);
}

function _gdToggleCheck(id, on) {
    _gdSetSubtree(id, on);
    _gdBubbleUp(id);
    _gdRefreshTopCheckbox();
}

function _gdSetSubtree(id, on) {
    const node = gdPickNodes.get(id);
    node.checked = on;
    node.indeterminate = false;
    if (node.children) {
        for (const cid of node.children) _gdSetSubtree(cid, on);
    }
    _gdReRenderCb(id);
}

function _gdBubbleUp(id) {
    // Find the parent of `id` by scanning the tree (simple enough for Drive folder counts).
    const parent = _gdFindParent(id);
    if (!parent) return;
    const pnode = gdPickNodes.get(parent);
    if (!pnode.children) return;
    const kids = pnode.children.map((cid) => gdPickNodes.get(cid));
    const allOn = kids.every((k) => k.checked && !k.indeterminate);
    const noneOn = kids.every((k) => !k.checked && !k.indeterminate);
    pnode.checked = allOn;
    pnode.indeterminate = !allOn && !noneOn;
    _gdReRenderCb(parent);
    _gdBubbleUp(parent);
}

function _gdFindParent(id) {
    for (const [pid, node] of gdPickNodes) {
        if (node.children && node.children.includes(id)) return pid;
    }
    return null;
}

function _gdReRenderRow(id) {
    // Replace the wrapper div for `id` (but not the subtree DOM — re-render from scratch).
    const old = document.querySelector(`[data-gd-id="${id}"]`);
    if (!old) return;
    const depth = Math.round((parseInt(old.querySelector(".gd-tree-row").style.paddingLeft) - 4) / 16);
    const fresh = _gdBuildRow(id, depth);
    old.replaceWith(fresh);
}

function _gdReRenderCb(id) {
    const node = gdPickNodes.get(id);
    const cb = document.querySelector(`[data-gd-id="${id}"] .gd-tree-cb`);
    if (!cb) return;
    cb.checked = node.checked;
    cb.indeterminate = node.indeterminate;
}

function _gdRefreshTopCheckbox() {
    const all = $("#gd-pick-all");
    const nodes = [...gdPickNodes.values()];
    if (!nodes.length) { all.checked = false; all.indeterminate = false; return; }
    const on = nodes.filter((n) => n.checked).length;
    all.checked = on === nodes.size;
    all.indeterminate = on > 0 && on < nodes.size;
}

function _gdSubtitle(f) {
    const parts = [];
    if (f.owner_email) parts.push(f.owner_email);
    if (f.shared_at) parts.push(`shared ${f.shared_at.slice(0, 10)}`);
    if (f.modified_at) parts.push(`modified ${f.modified_at.slice(0, 10)}`);
    return parts.join(" · ");
}

$("#gd-pick-all").addEventListener("change", () => {
    const on = $("#gd-pick-all").checked;
    for (const id of gdPickNodes.keys()) _gdSetSubtree(id, on);
    _gdRefreshTopCheckbox();
});

function closeGdPicker() {
    $("#gd-pick-backdrop").hidden = true;
}

$("#gd-pick-close").addEventListener("click", closeGdPicker);
$("#gd-pick-cancel").addEventListener("click", closeGdPicker);
$("#gd-pick-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "gd-pick-backdrop") closeGdPicker();
});

$("#gd-pick-ok").addEventListener("click", () => {
    // Collect every fully-checked node (not indeterminate). This gives us
    // the minimal set needed: if a parent is checked, no need to list children.
    const picked = [];
    const checkedIds = new Set(
        [...gdPickNodes.entries()]
            .filter(([, n]) => n.checked && !n.indeterminate)
            .map(([id]) => id)
    );
    // Drop descendants whose ancestor is already in the set.
    for (const id of checkedIds) {
        const isRedundant = [...checkedIds].some(
            (oid) => oid !== id && gdPickNodes.get(oid)?.children?.includes(id)
        );
        if (!isRedundant) {
            picked.push({ id, name: gdPickNodes.get(id).name });
        }
    }
    setGdFolders(picked);
    closeGdPicker();
});

// ---------------------------------------------------------------------------
// Handler plumbing (reset / load / after-save / provider-picker sequencing)
// ---------------------------------------------------------------------------

function gdReset() {
    $("#sync-gd-client-id").value = "";
    $("#sync-gd-client-secret").value = "";
    $("#sync-gd-client-secret").placeholder = "GOCSPX-…";
    $("#sync-gd-add-folder-id").value = "";
    setGdFolders([]);
    _snapshotSavedGdFolders([]);
    $("#sync-gd-sa-json").value = "";
    $("#sync-gd-sa-json").placeholder = '{"type":"service_account","client_email":"…","private_key":"…"}';
    $("#sync-gd-use-loopback").checked = false;
    $("#sync-gd-files-only").checked = false;
    $("#sync-gd-api-result").hidden = true;
    $("#sync-gd-test-hint").textContent = "";
    // Built-in Google client tab: offered only when this deploy ships one
    // (desktop builds). When it does, it's the zero-setup default for a
    // fresh form; loadSyncSource overrides from the saved row.
    const gdBuiltin = !!me.get()?.gd_builtin_available;
    $("#sync-gd-tab-builtin").hidden = !gdBuiltin;
    $("#sync-gd-conn-status-builtin").textContent = "Not connected";
    setGdAuthMode(gdBuiltin ? "builtin" : "oauth");
    setGdConnState({ connected: false, hasClientSecret: false });
    // Reset picker state; beforeLoad() then refreshes the provider list so
    // the pre-select-by-client_id pass in loadSyncSource sees a populated
    // gdGoogleProviders map.
    $("#sync-gd-provider-picker").value = "";
    $("#sync-gd-pane-oauth").classList.remove("provider-picked");
}

function loadGdForm(src) {
    const gd = src.google_drive;
    if (!gd) return;
    $("#sync-gd-client-id").value = gd.client_id || "";
    preselectGdProviderByClientId(gd.client_id || "");
    setGdFolders(gd.folders || []);
    _snapshotSavedGdFolders(gd.folders || []);
    $("#sync-gd-client-secret").placeholder = gd.has_client_secret ? "(saved — type to replace)" : "GOCSPX-…";
    $("#sync-gd-sa-json").placeholder = gd.has_service_account ? "(service account JSON saved — paste a new one to replace)" : '{"type":"service_account","client_email":"…","private_key":"…"}';
    $("#sync-gd-use-loopback").checked = !!gd.use_loopback;
    $("#sync-gd-files-only").checked = !!gd.files_only;
    $("#sync-gd-api-result").hidden = true;
    updateGdRedirectHint();
    // Pick the right tab. Built-in rows land on their own tab
    // (even when the deploy no longer offers it — the status
    // then explains why Connect fails). Otherwise: SA when the
    // saved config has only a service-account key, else OAuth.
    // Setting the mode AFTER populating the inputs so
    // setGdConnState reads the right placeholders.
    $("#sync-gd-conn-status-builtin").textContent =
        gd.connected ? "Connected ✓" : "Not connected";
    if (gd.use_builtin) {
        $("#sync-gd-tab-builtin").hidden = false;
        setGdAuthMode("builtin");
    } else {
        const saOnly = gd.has_service_account && !gd.has_client_secret;
        setGdAuthMode(saOnly ? "sa" : "oauth");
        setGdConnState({ connected: gd.connected, hasClientSecret: gd.has_client_secret });
    }
}

// Refresh credential placeholders after a save: typed-in secrets are now
// stored server-side, so we wipe the inputs (so a re-save doesn't re-send
// them) and flip the placeholders to the "(saved — type to replace)"
// wording. Same dance for both OAuth and SA panes — the user sees
// "I just saved, the field is blank now" + "(saved — paste again to
// replace)" instead of being unsure whether their paste persisted.
function gdAfterSave(out) {
    const gd = out.google_drive;
    if (!gd) return;
    $("#sync-gd-client-secret").value = "";
    $("#sync-gd-client-secret").placeholder = gd.has_client_secret
        ? "(saved — type to replace)"
        : "GOCSPX-…";
    $("#sync-gd-sa-json").value = "";
    $("#sync-gd-sa-json").placeholder = gd.has_service_account
        ? "(service account JSON saved — paste a new one to replace)"
        : '{"type":"service_account","client_email":"…","private_key":"…"}';
    // Folders the server now considers persisted — adopt them as
    // the canonical state so a subsequent Sync-now goes through
    // exactly what was saved (and we surface server-side renames
    // / dedupes in the next render).
    setGdFolders(gd.folders || []);
    _snapshotSavedGdFolders(gd.folders || []);
    $("#sync-gd-files-only").checked = !!gd.files_only;
    setGdConnState({ connected: gd.connected, hasClientSecret: gd.has_client_secret });
}

function gdAfterLoad() {
    // If loadSyncSource didn't match a provider (no saved config,
    // or saved client_id that doesn't match a row) AND there's
    // exactly one enabled Google provider, auto-pick it. Saves a
    // click in the common single-provider case without
    // overriding an existing sync's saved value.
    const sel = $("#sync-gd-provider-picker");
    const idInput = $("#sync-gd-client-id");
    if (!sel.value && !idInput.value && gdGoogleProviders.size === 1) {
        const onlyId = [...gdGoogleProviders.keys()][0];
        sel.value = String(onlyId);
        applyGdProviderSelection(onlyId);
    }
}

registerSource({
    type: "google_drive",
    tab: "google_drive",
    paneId: "#sync-form-google_drive",
    reset: gdReset,
    onShow: updateGdRedirectHint,
    load: loadGdForm,
    formConfig: gdFormConfig,
    afterSave: gdAfterSave,
    beforeLoad: refreshGdProviderPicker,
    afterLoad: gdAfterLoad,
    beforeSaveConfirm: gdBeforeSaveConfirm,
});
