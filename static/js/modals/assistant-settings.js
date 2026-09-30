// Settings → Assistant: LLM credentials and (super-admins) deployment policy.
//
// Everyone can store a personal Anthropic API key — it pays for their own
// questions. Super-admins additionally manage the deployment API key, the
// shared Claude subscription token and the assistant switch / defaults.
// Secrets are write-only: the server returns a masked hint, never the value.
// Rendered from the shared ``assistantConfig`` store, reloaded after every
// change so the chat window picks up engine availability immediately.

import { api } from "../api.js";
import { loadAssistantConfig } from "../assistant/config.js";
import { assistantConfig } from "../store.js";

const $ = (sel) => document.querySelector(sel);

const CREDENTIALS = [
    {
        key: "person_api_key",
        scope: "person",
        kind: "anthropic_api_key",
        label: "Your Anthropic API key",
        hint: "Used for your own questions instead of the deployment key.",
        placeholder: "sk-ant-api03-…",
        admin: false,
    },
    {
        key: "deployment_api_key",
        scope: "deployment",
        kind: "anthropic_api_key",
        label: "Deployment Anthropic API key",
        hint: "Used by everyone without a personal key.",
        placeholder: "sk-ant-api03-…",
        admin: true,
    },
    {
        key: "subscription",
        scope: "deployment",
        kind: "claude_oauth_token",
        label: "Claude subscription (super-admins)",
        hint: "Run `claude setup-token` on any machine with Claude Code, sign in, and paste the sk-ant-oat… token it prints. Shared by all super-admins.",
        placeholder: "sk-ant-oat01-…",
        admin: true,
    },
];

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

function statusText(s) {
    if (!s?.configured) return "Not set";
    const parts = [s.source === "environment" ? "Set in the environment" : `Set ${s.hint || ""}`.trim()];
    if (s.last_error) parts.push(`last check failed: ${s.last_error}`);
    else if (s.last_verified_at) parts.push(`verified ${new Date(s.last_verified_at * 1000).toLocaleString()}`);
    return parts.join(" — ");
}

function credentialRow(spec, status, isAdmin) {
    const row = el("div", "as-cred");
    row.append(el("div", "as-cred-label", spec.label), el("div", "hint as-cred-hint", spec.hint));
    const line = el("div", "as-cred-status", statusText(status));
    line.classList.toggle("is-error", !!status?.last_error);
    row.append(line);
    const readOnly = spec.admin && !isAdmin;
    if (readOnly) return row;

    const controls = el("div", "as-cred-controls");
    const inputEl = el("input");
    inputEl.type = "password";
    inputEl.autocomplete = "off";
    inputEl.spellcheck = false;
    inputEl.placeholder = spec.placeholder;
    const save = el("button", "btn btn-primary btn-sm", "Save");
    const test = el("button", "btn btn-secondary btn-sm", "Test");
    const remove = el("button", "btn btn-secondary btn-sm", "Remove");
    const fromEnv = status?.source === "environment";
    test.disabled = !status?.configured;
    remove.disabled = !status?.configured || fromEnv;
    const result = el("div", "hint as-cred-result");

    save.addEventListener("click", async () => {
        const secret = inputEl.value.trim();
        if (!secret) return;
        save.disabled = true;
        try {
            await api.assistantPutCredential(spec.scope, spec.kind, secret);
            inputEl.value = "";
            await loadAssistantConfig();
        } catch (err) {
            result.textContent = err.message;
        } finally {
            save.disabled = false;
        }
    });
    test.addEventListener("click", async () => {
        test.disabled = true;
        result.textContent = "Checking…";
        try {
            const r = await api.assistantTestCredential(spec.scope, spec.kind);
            result.textContent = {
                ok: "Works.",
                auth_failed: `Rejected: ${r.detail || "invalid credential"}`,
                inconclusive: `Couldn't tell: ${r.detail || "provider unreachable"}`,
            }[r.result];
            await loadAssistantConfig();
        } catch (err) {
            result.textContent = err.message;
        } finally {
            test.disabled = false;
        }
    });
    remove.addEventListener("click", async () => {
        if (!confirm(`Remove the ${spec.label.toLowerCase()}?`)) return;
        try {
            await api.assistantDeleteCredential(spec.scope, spec.kind);
            await loadAssistantConfig();
        } catch (err) {
            result.textContent = err.message;
        }
    });
    controls.append(inputEl, save, test, remove);
    row.append(controls, result);
    return row;
}

function policyBlock(cfg) {
    const block = el("div", "as-policy");
    block.append(el("div", "as-cred-label", "Deployment policy"));

    const enabled = el("label", "check-row as-policy-row");
    const box = el("input");
    box.type = "checkbox";
    box.checked = cfg.policy.enabled;
    enabled.append(box, " Assistant enabled for everyone");

    const model = el("select", "as-policy-select");
    for (const m of cfg.models) {
        const o = el("option", null, m.label);
        o.value = m.id;
        model.append(o);
    }
    model.value = cfg.policy.default_model;
    const effort = el("select", "as-policy-select");
    for (const e of cfg.efforts) {
        const o = el("option", null, e);
        o.value = e;
        effort.append(o);
    }
    effort.value = cfg.policy.default_effort;
    const defaults = el("div", "as-policy-row");
    defaults.append("Default model ", model, " effort ", effort);

    const save = async (patch) => {
        try {
            await api.assistantPolicy(patch);
            await loadAssistantConfig();
        } catch (err) {
            alert(err.message);
        }
    };
    box.addEventListener("change", () => save({ enabled: box.checked }));
    model.addEventListener("change", () => save({ default_model: model.value }));
    effort.addEventListener("change", () => save({ default_effort: effort.value }));
    block.append(enabled, defaults);
    return block;
}

function render(cfg) {
    const section = $("#assistant-settings");
    if (!section) return;
    section.hidden = !cfg;
    if (!cfg) return;
    const body = $("#assistant-settings-body");
    const rows = CREDENTIALS
        .filter((spec) => cfg.credentials[spec.key] !== undefined)
        .filter((spec) => !spec.admin || cfg.is_admin || spec.key === "deployment_api_key")
        .map((spec) => credentialRow(spec, cfg.credentials[spec.key], cfg.is_admin));
    body.replaceChildren(...rows);
    if (cfg.is_admin) body.append(policyBlock(cfg));
    if (!cfg.enabled) {
        body.prepend(el("p", "hint", "The assistant is turned off for this deployment."));
    }
}

// Re-render whenever the config changes while the modal is open.
assistantConfig.subscribe((cfg) => {
    if (!$("#settings-backdrop").hidden) render(cfg);
});

export async function refreshAssistantSettings() {
    await loadAssistantConfig();
    render(assistantConfig.get());
}
