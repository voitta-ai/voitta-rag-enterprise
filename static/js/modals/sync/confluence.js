// ---------------------------------------------------------------------------
// Confluence — mirrors the Jira wiring (same Cloud/Server auth split + shared
// openListPicker for the searchable multi-select spaces picker).
// ---------------------------------------------------------------------------

import { api } from "../../api.js";
import { _doSave } from "./core.js";
import { openListPicker, withPickerButtonBusy } from "./pickers.js";
import { registerSource } from "./registry.js";
import { $, ctx, SYNC_DEFAULT_SINCE } from "./state.js";

let cfSpaces = [];
// Whether credential fields changed since the last save (the spaces endpoint
// reads the stored row, so the picker re-saves only when dirty).
let cfFormDirty = false;

function setCfAuthMode(mode) {
    const m = mode === "server" ? "server" : "cloud";
    $("#sync-cf-tab-cloud").classList.toggle("active", m === "cloud");
    $("#sync-cf-tab-cloud").setAttribute("aria-selected", m === "cloud" ? "true" : "false");
    $("#sync-cf-tab-server").classList.toggle("active", m === "server");
    $("#sync-cf-tab-server").setAttribute("aria-selected", m === "server" ? "true" : "false");
    $("#sync-cf-email-row").hidden = m !== "cloud";
    $("#sync-cf-token-label").textContent = m === "cloud"
        ? "API token" : "Personal access token";
    const form = $("#sync-form-confluence");
    if (form) form.dataset.authMode = m;
    updateCfConnState();
}

function getCfAuthMode() {
    return $("#sync-form-confluence")?.dataset.authMode === "server" ? "server" : "cloud";
}

function loadConfluenceForm(cfg) {
    setCfAuthMode(cfg.auth_method || "cloud");
    $("#sync-cf-base-url").value = cfg.base_url || "";
    $("#sync-cf-email").value = cfg.email || "";
    $("#sync-cf-token").value = "";
    $("#sync-cf-token").placeholder = cfg.has_token
        ? "(saved — type to replace)" : "(paste API token / PAT)";
    $("#sync-cf-cql").value = cfg.cql || "";
    $("#sync-cf-updated-since").value = cfg.updated_since || cfg.default_since || "";
    $("#sync-cf-all-spaces").checked = !!cfg.all_spaces;
    setCfSpaces(cfg.spaces || []);
    cfFormDirty = false;
}

function confluenceFormConfig() {
    return {
        base_url: $("#sync-cf-base-url").value.trim(),
        auth_method: getCfAuthMode(),
        email: $("#sync-cf-email").value.trim(),
        token: $("#sync-cf-token").value,
        spaces: cfSpaces,
        all_spaces: !!$("#sync-cf-all-spaces").checked,
        cql: $("#sync-cf-cql").value.trim(),
        updated_since: $("#sync-cf-updated-since").value,
    };
}

function setCfSpaces(spaces) {
    cfSpaces = (spaces || []).map((s) => ({
        key: String(s.key || ""),
        name: s.name || s.key || "",
    })).filter((s) => s.key);
    updateCfSpacesUi();
}

function updateCfSpacesUi() {
    const list = $("#sync-cf-spaces-list");
    const count = $("#sync-cf-spaces-count");
    list.innerHTML = "";
    const allOn = $("#sync-cf-all-spaces")?.checked;
    if (allOn) {
        count.textContent = "all accessible spaces";
    } else if (cfSpaces.length === 0) {
        count.textContent = "none selected";
    } else {
        count.textContent =
            `${cfSpaces.length} space${cfSpaces.length === 1 ? "" : "s"} selected`;
        for (const s of cfSpaces) {
            const li = document.createElement("li");
            li.textContent = s.name && s.name !== s.key ? `${s.key} — ${s.name}` : s.key;
            list.append(li);
        }
    }
    updateCfConnState();
}

function updateCfConnState() {
    const mode = getCfAuthMode();
    const baseUrl = $("#sync-cf-base-url").value.trim();
    const emailOk = mode === "server" || $("#sync-cf-email").value.trim().length > 0;
    const tokenOk = $("#sync-cf-token").value.length > 0
        || $("#sync-cf-token").placeholder.startsWith("(saved");
    const ready = !!(baseUrl && emailOk && tokenOk);
    const allOn = $("#sync-cf-all-spaces").checked;
    const btn = $("#sync-cf-pick-spaces");
    btn.disabled = !ready || allOn;
    btn.title = allOn
        ? "Syncing all spaces — no selection needed"
        : (ready ? "" : "Enter base URL, token (and email for Cloud) first");
}

function confluenceAfterSave(cfg) {
    $("#sync-cf-token").value = "";
    $("#sync-cf-token").placeholder = cfg.has_token
        ? "(saved — type to replace)" : "(paste API token / PAT)";
    $("#sync-cf-all-spaces").checked = !!cfg.all_spaces;
    setCfSpaces(cfg.spaces || []);
    cfFormDirty = false;
}

async function confluencePickSpaces() {
    const btn = $("#sync-cf-pick-spaces");
    await withPickerButtonBusy(btn, async () => {
        if (cfFormDirty) {
            await _doSave();
            cfFormDirty = false;
        }
        openListPicker({
            title: "Pick Confluence spaces",
            multi: true,
            search: async (q) => {
                const resp = await api.confluenceListSpaces(ctx.folderId, q);
                return resp.spaces || [];
            },
            keyOf: (s) => s.key,
            primaryOf: (s) => s.key,
            secondaryOf: (s) => (s.name && s.name !== s.key ? s.name : ""),
            selectedKeys: cfSpaces.map((s) => s.key),
            seedItems: cfSpaces,
            onConfirm: (chosen) => {
                cfSpaces = chosen.map((s) => ({ key: s.key, name: s.name || s.key }));
                updateCfSpacesUi();
            },
        });
    });
}

$("#sync-cf-tab-cloud").addEventListener("click", () => { cfFormDirty = true; setCfAuthMode("cloud"); });
$("#sync-cf-tab-server").addEventListener("click", () => { cfFormDirty = true; setCfAuthMode("server"); });
$("#sync-cf-all-spaces").addEventListener("change", updateCfSpacesUi);
$("#sync-cf-pick-spaces").addEventListener("click", confluencePickSpaces);
for (const id of ["#sync-cf-base-url", "#sync-cf-email", "#sync-cf-token"]) {
    $(id).addEventListener("input", () => { cfFormDirty = true; updateCfConnState(); });
}

function confluenceReset() {
    // Confluence defaults — loadSyncSource overrides from the row when present.
    $("#sync-cf-base-url").value = "";
    $("#sync-cf-email").value = "";
    $("#sync-cf-token").value = "";
    $("#sync-cf-token").placeholder = "(paste API token / PAT)";
    $("#sync-cf-cql").value = "";
    $("#sync-cf-updated-since").value = SYNC_DEFAULT_SINCE;
    $("#sync-cf-all-spaces").checked = false;
    setCfSpaces([]);
    setCfAuthMode("cloud");
    cfFormDirty = false;
}

registerSource({
    type: "confluence",
    tab: "confluence",
    paneId: "#sync-form-confluence",
    reset: confluenceReset,
    onShow: () => setCfAuthMode(getCfAuthMode()),
    load: (src) => { if (src.confluence) loadConfluenceForm(src.confluence); },
    formConfig: confluenceFormConfig,
    afterSave: (out) => { if (out.confluence) confluenceAfterSave(out.confluence); },
});
