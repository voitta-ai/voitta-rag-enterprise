// ---------------------------------------------------------------------------
// Jira
// ---------------------------------------------------------------------------

import { api } from "../../api.js";
import { _doSave } from "./core.js";
import { openListPicker, withPickerButtonBusy } from "./pickers.js";
import { registerSource } from "./registry.js";
import { $, ctx, SYNC_DEFAULT_SINCE } from "./state.js";

let jiraProjects = [];
// Whether credential fields changed since the last save. The picker re-saves
// only when dirty (the project-list endpoint reads the stored row).
let jiraFormDirty = false;

function setJiraAuthMode(mode) {
    const m = mode === "server" ? "server" : "cloud";
    $("#sync-jira-tab-cloud").classList.toggle("active", m === "cloud");
    $("#sync-jira-tab-cloud").setAttribute("aria-selected", m === "cloud" ? "true" : "false");
    $("#sync-jira-tab-server").classList.toggle("active", m === "server");
    $("#sync-jira-tab-server").setAttribute("aria-selected", m === "server" ? "true" : "false");
    // Email is the Basic-auth username on Cloud; Server uses the PAT alone.
    $("#sync-jira-email-row").hidden = m !== "cloud";
    $("#sync-jira-token-label").textContent = m === "cloud"
        ? "API token" : "Personal access token";
    const form = $("#sync-form-jira");
    if (form) form.dataset.authMode = m;
    updateJiraConnState();
}

function getJiraAuthMode() {
    return $("#sync-form-jira")?.dataset.authMode === "server" ? "server" : "cloud";
}

function loadJiraForm(cfg) {
    setJiraAuthMode(cfg.auth_method || "cloud");
    $("#sync-jira-base-url").value = cfg.base_url || "";
    $("#sync-jira-email").value = cfg.email || "";
    $("#sync-jira-token").value = "";
    $("#sync-jira-token").placeholder = cfg.has_token
        ? "(saved — type to replace)" : "(paste API token / PAT)";
    $("#sync-jira-jql").value = cfg.jql || "";
    // Show the stored floor, else the connector's built-in default so the field
    // is never blank/ambiguous.
    $("#sync-jira-updated-since").value = cfg.updated_since || cfg.default_since || "";
    $("#sync-jira-all-projects").checked = !!cfg.all_projects;
    setJiraProjects(cfg.projects || []);
    // Freshly loaded from the server → credentials match what's stored, so the
    // picker needn't re-save before listing.
    jiraFormDirty = false;
}

function jiraFormConfig() {
    return {
        base_url: $("#sync-jira-base-url").value.trim(),
        auth_method: getJiraAuthMode(),
        email: $("#sync-jira-email").value.trim(),
        token: $("#sync-jira-token").value,
        projects: jiraProjects,
        all_projects: !!$("#sync-jira-all-projects").checked,
        jql: $("#sync-jira-jql").value.trim(),
        updated_since: $("#sync-jira-updated-since").value,
    };
}

function setJiraProjects(projects) {
    jiraProjects = (projects || []).map((p) => ({
        key: String(p.key || ""),
        name: p.name || p.key || "",
    })).filter((p) => p.key);
    updateJiraProjectsUi();
}

function updateJiraProjectsUi() {
    const list = $("#sync-jira-projects-list");
    const count = $("#sync-jira-projects-count");
    list.innerHTML = "";
    const allOn = $("#sync-jira-all-projects")?.checked;
    if (allOn) {
        count.textContent = "all accessible projects";
    } else if (jiraProjects.length === 0) {
        count.textContent = "none selected";
    } else {
        count.textContent =
            `${jiraProjects.length} project${jiraProjects.length === 1 ? "" : "s"} selected`;
        for (const p of jiraProjects) {
            const li = document.createElement("li");
            li.textContent = p.name && p.name !== p.key ? `${p.key} — ${p.name}` : p.key;
            list.append(li);
        }
    }
    updateJiraConnState();
}

function updateJiraConnState() {
    const mode = getJiraAuthMode();
    const baseUrl = $("#sync-jira-base-url").value.trim();
    const emailOk = mode === "server" || $("#sync-jira-email").value.trim().length > 0;
    const tokenOk = $("#sync-jira-token").value.length > 0
        || $("#sync-jira-token").placeholder.startsWith("(saved");
    const ready = !!(baseUrl && emailOk && tokenOk);
    const allOn = $("#sync-jira-all-projects").checked;
    const btn = $("#sync-jira-pick-projects");
    btn.disabled = !ready || allOn;
    btn.title = allOn
        ? "Syncing all projects — no selection needed"
        : (ready ? "" : "Enter base URL, token (and email for Cloud) first");
}

function jiraAfterSave(cfg) {
    $("#sync-jira-token").value = "";
    $("#sync-jira-token").placeholder = cfg.has_token
        ? "(saved — type to replace)" : "(paste API token / PAT)";
    $("#sync-jira-all-projects").checked = !!cfg.all_projects;
    setJiraProjects(cfg.projects || []);
    jiraFormDirty = false;  // just persisted — stored row matches the form
}

async function jiraPickProjects() {
    const btn = $("#sync-jira-pick-projects");
    await withPickerButtonBusy(btn, async () => {
        // The list endpoint reads the persisted row, so save first — but only
        // when something actually changed (avoids a needless PUT round trip on
        // every open).
        if (jiraFormDirty) {
            await _doSave();
            jiraFormDirty = false;
        }
        // Server-side search: the picker queries Jira on each keystroke instead
        // of downloading every project (enterprise orgs have thousands), so the
        // list appears immediately and never freezes the tab.
        openListPicker({
            title: "Pick Jira projects",
            multi: true,
            search: async (q) => {
                const resp = await api.jiraListProjects(ctx.folderId, q);
                return resp.projects || [];
            },
            keyOf: (p) => p.key,
            primaryOf: (p) => p.key,
            secondaryOf: (p) => (p.name && p.name !== p.key ? p.name : ""),
            selectedKeys: jiraProjects.map((p) => p.key),
            seedItems: jiraProjects,
            onConfirm: (chosen) => {
                jiraProjects = chosen.map((p) => ({ key: p.key, name: p.name || p.key }));
                updateJiraProjectsUi();
            },
        });
    });
}

$("#sync-jira-tab-cloud").addEventListener("click", () => { jiraFormDirty = true; setJiraAuthMode("cloud"); });
$("#sync-jira-tab-server").addEventListener("click", () => { jiraFormDirty = true; setJiraAuthMode("server"); });
$("#sync-jira-all-projects").addEventListener("change", updateJiraProjectsUi);
$("#sync-jira-pick-projects").addEventListener("click", jiraPickProjects);
// Credential edits mark the form dirty so the picker re-saves before listing;
// other fields (projects, jql) don't affect the project-list query.
for (const id of ["#sync-jira-base-url", "#sync-jira-email", "#sync-jira-token"]) {
    $(id).addEventListener("input", () => { jiraFormDirty = true; updateJiraConnState(); });
}

function jiraReset() {
    // Jira defaults — loadSyncSource overrides from the row when present.
    $("#sync-jira-base-url").value = "";
    $("#sync-jira-email").value = "";
    $("#sync-jira-token").value = "";
    $("#sync-jira-token").placeholder = "(paste API token / PAT)";
    $("#sync-jira-jql").value = "";
    $("#sync-jira-updated-since").value = SYNC_DEFAULT_SINCE;
    $("#sync-jira-all-projects").checked = false;
    setJiraProjects([]);
    setJiraAuthMode("cloud");
    jiraFormDirty = false;
}

registerSource({
    type: "jira",
    tab: "jira",
    paneId: "#sync-form-jira",
    reset: jiraReset,
    onShow: () => setJiraAuthMode(getJiraAuthMode()),
    load: (src) => { if (src.jira) loadJiraForm(src.jira); },
    formConfig: jiraFormConfig,
    afterSave: (out) => { if (out.jira) jiraAfterSave(out.jira); },
});
