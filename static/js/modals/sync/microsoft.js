// ===========================================================================
// Microsoft (SharePoint + Teams) — auth tabs, sites picker, scope warning.
//
// Same shape for both connectors: shared auth fields (tenant/client/secret/
// cert) live behind the ``sp-`` / ``tm-`` element prefix to keep the two
// forms independent. Form helpers below are written generically — ``kind``
// is "sp" for SharePoint and "tm" for Teams. This module registers TWO
// handlers (sharepoint, teams) that share its module-private helpers.
// ===========================================================================

import { api } from "../../api.js";
import { _doSave, loadSyncSource } from "./core.js";
import { openListPicker, withPickerButtonBusy } from "./pickers.js";
import { registerSource } from "./registry.js";
import { $, ctx } from "./state.js";

const MS_LOOPBACK_REDIRECT_URI =
    "http://localhost:53682/api/sync/oauth/microsoft/callback";

// Admin-saved Microsoft providers, populated lazily from
// /api/admin/auth-providers (readable by any signed-in user). Reused by both the SharePoint
// and Teams provider pickers — same tenant id is a valid pick for either
// connector. Map<id, {id, label, tenant_id, client_id, client_secret, source}>.
const msMicrosoftProviders = new Map();

async function refreshMsProviderPickers() {
    // Reset both pickers + the shared cache, then attempt the admin call.
    // Non-admins (403) silently keep the pickers hidden.
    msMicrosoftProviders.clear();
    for (const kind of ["sp", "tm"]) {
        const sel = $(`#sync-${kind}-provider-picker`);
        sel.innerHTML = '<option value="">— Manual entry —</option>';
        $(`#sync-${kind}-provider-row`).hidden = true;
    }
    let providers;
    try {
        providers = await api.adminListAuthProviders();
    } catch (err) {
        if (!String(err.message || "").startsWith("403")) {
            console.warn("MS provider list failed", err);
        }
        return;
    }
    const enabled = (providers || []).filter(
        (p) => p.provider === "microsoft" && p.enabled
    );
    if (!enabled.length) return;
    for (const p of enabled) {
        msMicrosoftProviders.set(p.id, p);
    }
    for (const kind of ["sp", "tm"]) {
        const sel = $(`#sync-${kind}-provider-picker`);
        for (const p of enabled) {
            const opt = document.createElement("option");
            opt.value = String(p.id);
            const tail = p.source === "env" ? "  (.env)" : "";
            opt.textContent = (p.label || p.client_id) + tail;
            sel.append(opt);
        }
        $(`#sync-${kind}-provider-row`).hidden = false;
    }
}

function applyMsProviderSelection(kind, providerId) {
    if (!providerId) return;
    const p = msMicrosoftProviders.get(Number(providerId));
    if (!p) return;
    const tenantInput = $(`#sync-${kind}-tenant-id`);
    const cidInput = $(`#sync-${kind}-client-id`);
    const secretInput = $(`#sync-${kind}-client-secret`);
    tenantInput.value = p.tenant_id || "";
    cidInput.value = p.client_id || "";
    secretInput.value = p.client_secret || "";
    // Fire ``input`` so setMsConnState picks up the new values and
    // enables Connect.
    tenantInput.dispatchEvent(new Event("input", { bubbles: true }));
    cidInput.dispatchEvent(new Event("input", { bubbles: true }));
    secretInput.dispatchEvent(new Event("input", { bubbles: true }));
}

function preselectMsProviderByClientId(kind, clientId) {
    const sel = $(`#sync-${kind}-provider-picker`);
    if (!clientId) { sel.value = ""; return; }
    for (const p of msMicrosoftProviders.values()) {
        if (p.client_id === clientId) {
            sel.value = String(p.id);
            return;
        }
    }
    sel.value = "";
}

// Selected sites for SharePoint (mirrors the gdFolders array). Mutated by
// the picker modal + the saved-row loader.
let spSites = [];

function setSpSites(sites) {
    spSites = (sites || []).map((s) => ({
        id: String(s.id || ""),
        displayName: s.displayName || s.name || "",
        webUrl: s.webUrl || "",
    })).filter((s) => s.id);
    updateSpSitesUi();
}

function updateSpSitesUi() {
    const list = $("#sync-sp-sites-list");
    const count = $("#sync-sp-sites-count");
    list.innerHTML = "";
    if (spSites.length === 0) {
        count.textContent = "none selected";
    } else {
        count.textContent = `${spSites.length} site${spSites.length === 1 ? "" : "s"} selected`;
        for (const s of spSites) {
            const li = document.createElement("li");
            li.textContent = s.displayName || s.id;
            if (s.webUrl) li.title = s.webUrl;
            list.append(li);
        }
    }
    const allOn = $("#sync-sp-all-sites")?.checked;
    $("#sync-sp-pick-sites").disabled = allOn;
}

function updateTmUserModeUi() {
    const mode = document.querySelector('input[name="sync-tm-user-mode"]:checked')?.value || "me";
    $("#sync-tm-user-row").hidden = mode !== "specific";
    $("#sync-tm-pick-user").disabled = mode !== "specific";
}

function setMsAuthMode(kind, mode) {
    const tabs = {
        oauth: $(`#sync-${kind}-tab-oauth`),
        secret: $(`#sync-${kind}-tab-secret`),
        cert: $(`#sync-${kind}-tab-cert`),
    };
    for (const [k, el] of Object.entries(tabs)) {
        if (!el) continue;
        el.classList.toggle("active", k === mode);
        el.setAttribute("aria-selected", k === mode ? "true" : "false");
    }
    const secretPane = $(`#sync-${kind}-pane-secret`);
    const certPane = $(`#sync-${kind}-pane-cert`);
    if (secretPane) secretPane.hidden = mode === "cert";
    if (certPane) certPane.hidden = mode !== "cert";
    // Stash the mode on the wrapper element so form-config can read it.
    const root = $(`#sync-form-${kind === "sp" ? "sharepoint" : "teams"}`);
    if (root) root.dataset.authMode = mode;
}

function getMsAuthMode(kind) {
    const root = $(`#sync-form-${kind === "sp" ? "sharepoint" : "teams"}`);
    return root?.dataset.authMode || "oauth";
}

function msAuthMethodFromMode(mode) {
    if (mode === "secret") return "app_secret";
    if (mode === "cert") return "app_cert";
    return "oauth";
}

function modeFromAuthMethod(method) {
    if (method === "app_secret") return "secret";
    if (method === "app_cert") return "cert";
    return "oauth";
}

function updateMsLoopbackHint(kind) {
    const useLoopback = !!$(`#sync-${kind}-use-loopback`)?.checked;
    const hint = $(`#sync-${kind}-loopback-hint`);
    if (hint) hint.hidden = !useLoopback;
}

function setMsConnState(kind, { connected, hasSecret, hasCert }) {
    const mode = getMsAuthMode(kind);
    const tenant = $(`#sync-${kind}-tenant-id`).value.trim();
    const cid = $(`#sync-${kind}-client-id`).value.trim();
    let canConnect = false;
    if (mode === "oauth") {
        const secretReady = hasSecret || $(`#sync-${kind}-client-secret`).value.length > 0;
        canConnect = !!(tenant && cid && secretReady);
    }
    const btn = $(`#sync-${kind}-connect`);
    if (btn) {
        btn.disabled = !canConnect || mode !== "oauth";
        btn.title = mode !== "oauth"
            ? "App-only auth doesn't need a browser sign-in"
            : (canConnect ? "" : "Save tenant_id, client_id and client_secret first");
    }
    const status = $(`#sync-${kind}-conn-status`);
    if (status) {
        if (mode !== "oauth") {
            status.textContent = "App-only — no sign-in needed";
        } else if (connected) {
            status.textContent = "Connected ✓";
        } else {
            status.textContent = "";
        }
    }
    // Sites picker / user picker need a connection (or app-only creds).
    const ready = (mode !== "oauth") || connected;
    if (kind === "sp") {
        const allOn = $("#sync-sp-all-sites").checked;
        $("#sync-sp-pick-sites").disabled = !ready || allOn;
    } else {
        $("#sync-tm-pick-user").disabled = !ready
            || (document.querySelector('input[name="sync-tm-user-mode"]:checked')?.value !== "specific");
    }
}

function loadMsForm(kind, cfg) {
    const mode = modeFromAuthMethod(cfg.auth_method || "oauth");
    setMsAuthMode(kind, mode);
    $(`#sync-${kind}-tenant-id`).value = cfg.tenant_id || "";
    $(`#sync-${kind}-client-id`).value = cfg.client_id || "";
    preselectMsProviderByClientId(kind, cfg.client_id || "");
    $(`#sync-${kind}-client-secret`).placeholder = cfg.has_client_secret
        ? "(saved — type to replace)" : "(paste app secret)";
    const certEl = $(`#sync-${kind}-cert-pem`);
    if (certEl) certEl.placeholder = cfg.has_cert
        ? "(certificate saved — paste new PEM to replace)"
        : certEl.getAttribute("placeholder");
    $(`#sync-${kind}-use-loopback`).checked = !!cfg.use_loopback;
    updateMsLoopbackHint(kind);
    setMsConnState(kind, {
        connected: !!cfg.connected,
        hasSecret: !!cfg.has_client_secret,
        hasCert: !!cfg.has_cert,
    });
}

function msFormConfigGeneric(kind) {
    const mode = getMsAuthMode(kind);
    return {
        tenant_id: $(`#sync-${kind}-tenant-id`).value.trim(),
        client_id: $(`#sync-${kind}-client-id`).value.trim(),
        client_secret: $(`#sync-${kind}-client-secret`).value,
        cert_pem: $(`#sync-${kind}-cert-pem`)?.value || "",
        auth_method: msAuthMethodFromMode(mode),
        use_loopback: !!$(`#sync-${kind}-use-loopback`).checked,
    };
}

function spFormConfig() {
    return {
        ...msFormConfigGeneric("sp"),
        sites: spSites,
        all_sites: !!$("#sync-sp-all-sites").checked,
    };
}

function tmFormConfig() {
    const mode = document.querySelector('input[name="sync-tm-user-mode"]:checked')?.value || "me";
    return {
        ...msFormConfigGeneric("tm"),
        user_mode: mode,
        user_id: $("#sync-tm-user-id").value.trim(),
        include_attended: !!$("#sync-tm-include-attended").checked,
    };
}

// ---------------------------------------------------------------------------
// Wire up tabs + change handlers
// ---------------------------------------------------------------------------

for (const kind of ["sp", "tm"]) {
    $(`#sync-${kind}-tab-oauth`).addEventListener("click", () => setMsAuthMode(kind, "oauth"));
    $(`#sync-${kind}-tab-secret`).addEventListener("click", () => setMsAuthMode(kind, "secret"));
    $(`#sync-${kind}-tab-cert`).addEventListener("click", () => setMsAuthMode(kind, "cert"));
    for (const id of [`#sync-${kind}-tenant-id`, `#sync-${kind}-client-id`, `#sync-${kind}-client-secret`]) {
        $(id).addEventListener("input", () => setMsConnState(kind, {
            connected: $(`#sync-${kind}-conn-status`).textContent.startsWith("Connected"),
            hasSecret: $(`#sync-${kind}-client-secret`).placeholder.startsWith("(saved"),
        }));
    }
    $(`#sync-${kind}-use-loopback`).addEventListener("change", () => updateMsLoopbackHint(kind));
}

$("#sync-sp-all-sites").addEventListener("change", updateSpSitesUi);
document.querySelectorAll('input[name="sync-tm-user-mode"]').forEach((el) => {
    el.addEventListener("change", updateTmUserModeUi);
});

// Microsoft provider picker — selecting a row prefills tenant/client/
// secret in the corresponding pane. Same wiring for SP and TM.
$("#sync-sp-provider-picker").addEventListener("change", (e) =>
    applyMsProviderSelection("sp", e.target.value));
$("#sync-tm-provider-picker").addEventListener("change", (e) =>
    applyMsProviderSelection("tm", e.target.value));

// ---------------------------------------------------------------------------
// Connect (OAuth popup) + sites picker + user picker
// ---------------------------------------------------------------------------

async function msConnectFlow(kind) {
    // Same flow as gdConnectFlow: save first so the row has tenant/client/
    // secret, then open the OAuth URL in a popup and wait for the events
    // stream to notify us that the callback completed.
    try {
        await _doSave();
    } catch (err) {
        alert(err.message);
        return;
    }
    let auth_url;
    try {
        ({ auth_url } = await api.msAuthInit(ctx.folderId));
    } catch (err) {
        alert(err.message);
        return;
    }
    const popup = window.open(auth_url, "voitta-ms-auth", "width=520,height=720");
    if (!popup) {
        alert("Pop-up blocked — allow pop-ups and try again.");
        return;
    }
    // Same polling pattern as gdConnect — poll until the popup closes
    // then re-fetch the source row (the callback set the refresh_token
    // server-side before closing its own tab).
    const t = setInterval(async () => {
        if (popup.closed) {
            clearInterval(t);
            await loadSyncSource();
            msRunScopeCheck(kind).catch(() => {});
        }
    }, 500);
}

$("#sync-sp-connect").addEventListener("click", () => msConnectFlow("sp"));
$("#sync-tm-connect").addEventListener("click", () => msConnectFlow("tm"));

async function msRunScopeCheck(kind) {
    const panel = $(`#sync-${kind}-scope-warn`);
    if (!panel) return;
    try {
        const out = await api.msScopeCheck(ctx.folderId);
        renderScopeWarning(panel, out);
    } catch (err) {
        panel.hidden = false;
        panel.innerHTML = "";
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = `Scope check failed: ${err.message}`;
        panel.appendChild(p);
    }
}

function renderScopeWarning(panel, out) {
    panel.innerHTML = "";
    if (!out.missing || out.missing.length === 0) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;
    const h = document.createElement("strong");
    h.textContent = out.app_only
        ? "Missing application permissions — ask your Azure AD admin to grant:"
        : "Missing delegated permissions — reconnect after granting:";
    panel.appendChild(h);
    const ul = document.createElement("ul");
    for (const m of out.missing) {
        const li = document.createElement("li");
        li.innerHTML = `<code>${m.scope}</code> — ${m.feature}. <em>${m.impact}</em>`;
        ul.appendChild(li);
    }
    panel.appendChild(ul);
}

// SharePoint sites picker — loads the full site list (tenants have few enough
// sites that client-side filtering is fine), toggles into spSites.
async function msPickSites() {
    const btn = $("#sync-sp-pick-sites");
    await withPickerButtonBusy(btn, async () => {
        const resp = await api.msListSites(ctx.folderId);
        const all = resp.sites || [];
        openListPicker({
            title: "Pick SharePoint sites",
            multi: true,
            items: all,
            keyOf: (s) => s.id,
            primaryOf: (s) => s.displayName || s.id,
            secondaryOf: (s) => s.webUrl || "",
            // SP site "keys" are opaque GUIDs nobody pastes; exact multi-value
            // matching compares the human-meaningful site name instead.
            exactKeyOf: (s) => s.displayName || s.id,
            selectedKeys: spSites.map((s) => s.id),
            seedItems: spSites,
            onConfirm: (chosen) => {
                spSites = chosen.map((s) => ({
                    id: s.id, displayName: s.displayName || "", webUrl: s.webUrl || "",
                }));
                updateSpSitesUi();
            },
        });
    });
}
$("#sync-sp-pick-sites").addEventListener("click", msPickSites);

// Teams user picker — single select.
async function msPickUser() {
    const btn = $("#sync-tm-pick-user");
    await withPickerButtonBusy(btn, async () => {
        const resp = await api.msListUsers(ctx.folderId);
        const all = resp.users || [];
        openListPicker({
            title: "Pick a user",
            multi: false,
            items: all,
            keyOf: (u) => u.userPrincipalName || u.id,
            primaryOf: (u) => u.displayName || u.userPrincipalName,
            secondaryOf: (u) => u.userPrincipalName || u.mail || "",
            onConfirm: ([u]) => { $("#sync-tm-user-id").value = u.userPrincipalName || u.id; },
        });
    });
}
$("#sync-tm-pick-user").addEventListener("click", msPickUser);

// Post-save refresh for the MS panes — registered as ``afterSave`` on both
// handlers below; core's _doSave dispatches to it by ``out.source_type``.
function msAfterSave(out) {
    if (out.source_type === "sharepoint" && out.sharepoint) {
        $("#sync-sp-client-secret").value = "";
        $("#sync-sp-client-secret").placeholder = out.sharepoint.has_client_secret
            ? "(saved — type to replace)" : "(paste app secret)";
        $("#sync-sp-cert-pem").value = "";
        setMsConnState("sp", {
            connected: out.sharepoint.connected,
            hasSecret: out.sharepoint.has_client_secret,
            hasCert: out.sharepoint.has_cert,
        });
        setSpSites(out.sharepoint.sites || []);
        $("#sync-sp-all-sites").checked = !!out.sharepoint.all_sites;
        updateSpSitesUi();
        msRunScopeCheck("sp").catch(() => {});
    }
    if (out.source_type === "teams" && out.teams) {
        $("#sync-tm-client-secret").value = "";
        $("#sync-tm-client-secret").placeholder = out.teams.has_client_secret
            ? "(saved — type to replace)" : "(paste app secret)";
        $("#sync-tm-cert-pem").value = "";
        setMsConnState("tm", {
            connected: out.teams.connected,
            hasSecret: out.teams.has_client_secret,
            hasCert: out.teams.has_cert,
        });
        msRunScopeCheck("tm").catch(() => {});
    }
}

// ---------------------------------------------------------------------------
// Handler plumbing
// ---------------------------------------------------------------------------

function msReset() {
    // Reset MS picker selections; the populate path fires the admin call
    // once for both connectors (so this shared reset is registered on the
    // sharepoint handler only). ``loadSyncSource`` calls ``loadMsForm``
    // which then preselects-by-client_id.
    for (const kind of ["sp", "tm"]) {
        const sel = $(`#sync-${kind}-provider-picker`);
        if (sel) sel.value = "";
    }
    refreshMsProviderPickers();
}

function loadSpForm(src) {
    if (!src.sharepoint) return;
    loadMsForm("sp", src.sharepoint);
    setSpSites(src.sharepoint.sites || []);
    $("#sync-sp-all-sites").checked = !!src.sharepoint.all_sites;
    updateSpSitesUi();
}

function loadTmForm(src) {
    if (!src.teams) return;
    loadMsForm("tm", src.teams);
    const mode = src.teams.user_mode || "me";
    document.querySelectorAll('input[name="sync-tm-user-mode"]').forEach((el) => {
        el.checked = el.value === mode;
    });
    $("#sync-tm-user-id").value = src.teams.user_id || "";
    $("#sync-tm-include-attended").checked = !!src.teams.include_attended;
    updateTmUserModeUi();
}

registerSource({
    type: "sharepoint",
    tab: "sharepoint",
    paneId: "#sync-form-sharepoint",
    reset: msReset,  // shared for the SP/TM pair — see comment in msReset
    onShow: () => updateMsLoopbackHint("sp"),
    load: loadSpForm,
    formConfig: spFormConfig,
    afterSave: msAfterSave,
});

registerSource({
    type: "teams",
    tab: "teams",
    paneId: "#sync-form-teams",
    onShow: () => updateMsLoopbackHint("tm"),
    load: loadTmForm,
    formConfig: tmFormConfig,
    afterSave: msAfterSave,
});
