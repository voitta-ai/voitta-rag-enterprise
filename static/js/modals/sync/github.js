// GitHub sync section. Branch selection requires hitting the remote
// (POST /sync/branches) — the user fills in repo + auth, clicks
// "Load branches", then picks from the dropdown.

import { api } from "../../api.js";
import { registerSource } from "./registry.js";
import { $, ctx } from "./state.js";

function setGhAuth(method) {
    $("#sync-gh-ssh").hidden = method !== "ssh";
    $("#sync-gh-token").hidden = method !== "token";
    // Agent mode has no credential input — it uses the host's ssh-agent.
    const agentEl = $("#sync-gh-agent");
    if (agentEl) agentEl.hidden = method !== "agent";
    document.querySelectorAll('input[name="sync-gh-auth"]').forEach((el) => {
        el.checked = el.value === method;
    });
}

function ghFormConfig() {
    const authMethod = document.querySelector('input[name="sync-gh-auth"]:checked')?.value || "ssh";
    const sel = $("#sync-gh-branches");
    const branches = [...sel.selectedOptions].map((o) => o.value);
    return {
        repo: $("#sync-gh-repo").value.trim(),
        path: $("#sync-gh-path").value.trim(),
        auth_method: authMethod,
        username: $("#sync-gh-username").value.trim(),
        pat: $("#sync-gh-pat").value,           // not trimmed — preserve
        ssh_key: $("#sync-gh-ssh-key").value,   // not trimmed — preserve
        branches,
        all_branches: $("#sync-gh-all-branches").checked,
        extended: $("#sync-gh-extended").checked,
    };
}

function ghReset() {
    $("#sync-gh-repo").value = "";
    $("#sync-gh-path").value = "";
    setGhAuth("ssh");
    $("#sync-gh-ssh-key").value = "";
    $("#sync-gh-username").value = "";
    $("#sync-gh-pat").value = "";
    $("#sync-gh-all-branches").checked = false;
    $("#sync-gh-branches").innerHTML = "";
    $("#sync-gh-extended").checked = false;
}

function loadGhForm(src) {
    const gh = src.github;
    if (!gh) return;
    $("#sync-gh-repo").value = gh.repo || "";
    $("#sync-gh-path").value = gh.path || "";
    setGhAuth(gh.auth_method || "ssh");
    $("#sync-gh-username").value = gh.username || "";
    $("#sync-gh-pat").placeholder = gh.has_pat ? "(token saved — type to replace)" : "ghp_…";
    $("#sync-gh-ssh-key").placeholder = gh.has_ssh_key ? "(SSH key saved — paste a new one to replace)" : "-----BEGIN OPENSSH PRIVATE KEY-----…";
    $("#sync-gh-all-branches").checked = !!gh.all_branches;
    $("#sync-gh-extended").checked = !!gh.extended;
    // Pre-populate branches dropdown with the saved selection so the
    // user sees what's currently configured even before hitting "Load".
    const sel = $("#sync-gh-branches");
    sel.innerHTML = "";
    for (const b of gh.branches || []) {
        const opt = document.createElement("option");
        opt.value = b;
        opt.textContent = b;
        opt.selected = true;
        sel.append(opt);
    }
}

document.querySelectorAll('input[name="sync-gh-auth"]').forEach((el) => {
    el.addEventListener("change", () => setGhAuth(el.value));
});

$("#sync-gh-load-branches").addEventListener("click", async () => {
    const cfg = ghFormConfig();
    if (!cfg.repo) { alert("Enter a repo URL first."); return; }
    const btn = $("#sync-gh-load-branches");
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Loading…";
    try {
        const r = await api.listGitBranches(ctx.folderId, {
            repo: cfg.repo,
            auth_method: cfg.auth_method,
            username: cfg.username,
            pat: cfg.pat,
            ssh_key: cfg.ssh_key,
        });
        const sel = $("#sync-gh-branches");
        const previouslySelected = new Set([...sel.selectedOptions].map((o) => o.value));
        sel.innerHTML = "";
        for (const b of r.branches) {
            const opt = document.createElement("option");
            opt.value = b;
            opt.textContent = b;
            opt.selected = previouslySelected.has(b);
            sel.append(opt);
        }
        if (sel.selectedOptions.length === 0 && sel.options.length > 0) {
            sel.options[0].selected = true; // default to first (main / master)
        }
    } catch (err) {
        alert(err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = prev;
    }
});

registerSource({
    type: "github",
    tab: "github",
    paneId: "#sync-form-github",
    reset: ghReset,
    load: loadGhForm,
    formConfig: ghFormConfig,
});
