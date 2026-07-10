// User settings modal — personal API keys + (admins only) company keys.
//
// Each key is shown once at creation; the modal renders a copy
// affordance + ready-made Claude / Claude CLI snippets so the user
// can wire up MCP without hand-assembling URLs. Subsequent renders
// only show the prefix — there's no recover-the-token path.
//
// The company-keys section is scoped to the ACTIVE account's company
// (or the native space for Personal). Visibility is decided server-side:
// GET /auth/company-keys answers 403 for non-admins and the section
// stays hidden — no role logic client-side.

import { api } from "../api.js";
import { keysState, me } from "../store.js";

const $ = (sel) => document.querySelector(sel);

export function openSettings() {
    wireKeysStore();
    $("#settings-backdrop").hidden = false;
    $("#key-reveal").hidden = true;
    $("#key-name").value = "";
    $("#company-keys-section").hidden = true;
    $("#company-key-reveal").hidden = true;
    $("#company-key-name").value = "";
    // Render from the live store; the WS delivers the user's keys on connect
    // and re-pushes after every create/delete. No HTTP fetch on open.
    renderKeys(keysState.get());
    loadCompanyKeys();
}

export function closeSettings() {
    $("#settings-backdrop").hidden = true;
}

// Re-render the (open) keys panel whenever the user's key set changes. Wired
// once. Keys aren't editable inline, so a plain re-render is safe.
let keysStoreWired = false;
function wireKeysStore() {
    if (keysStoreWired) return;
    keysStoreWired = true;
    keysState.subscribe((keys) => {
        if ($("#settings-backdrop").hidden) return;
        renderKeys(keys);
    });
}

function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleString();
}

function renderKeys(keys) {
    keys = keys || [];
    const tbody = $("#keys-tbody");
    tbody.innerHTML = "";
    $("#keys-empty").hidden = keys.length > 0;
    for (const k of keys) {
        const tr = document.createElement("tr");
        tr.style.borderBottom = "1px solid var(--border, #eee)";
        tr.innerHTML = `
            <td style="padding:6px 8px;">${escapeHtml(k.name)}</td>
            <td style="padding:6px 8px;font-family:monospace;">${escapeHtml(k.prefix)}…</td>
            <td style="padding:6px 8px;">${fmtTime(k.created_at)}</td>
            <td style="padding:6px 8px;">${fmtTime(k.last_used_at)}</td>
            <td style="padding:6px 8px;text-align:right;"></td>
        `;
        const del = document.createElement("button");
        del.className = "btn btn-secondary btn-sm";
        del.textContent = "Delete";
        del.addEventListener("click", () => deleteKey(k));
        tr.lastElementChild.append(del);
        tbody.append(tr);
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]
    ));
}

async function deleteKey(k) {
    if (!confirm(`Delete key "${k.name}"?\n\nAny client still using its token will stop working immediately.`)) return;
    try {
        await api.deleteKey(k.id);
        // No refetch — the server pushes a fresh keys.snapshot; the store
        // subscription re-renders the table.
    } catch (err) {
        alert(err.message);
    }
}

// ----- Company keys (admins of the active scope only) -----

async function loadCompanyKeys() {
    // Refreshes the section (visibility + table) WITHOUT touching the
    // reveal banner — create calls this right after showing the token.
    const section = $("#company-keys-section");
    let data;
    try {
        data = await api.listCompanyKeys();
    } catch {
        // 403 (not an admin of this scope) or any transport error: no section.
        return;
    }
    const scope = data.company_id ? data.company_name : "Native space";
    $("#company-keys-title").textContent = `Company API keys — ${scope}`;
    renderCompanyKeys(data.keys);
    section.hidden = false;
}

function renderCompanyKeys(keys) {
    keys = keys || [];
    const tbody = $("#company-keys-tbody");
    tbody.innerHTML = "";
    $("#company-keys-empty").hidden = keys.length > 0;
    for (const k of keys) {
        const tr = document.createElement("tr");
        tr.style.borderBottom = "1px solid var(--border, #eee)";
        tr.innerHTML = `
            <td style="padding:6px 8px;">${escapeHtml(k.name)}</td>
            <td style="padding:6px 8px;font-family:monospace;">${escapeHtml(k.prefix)}…</td>
            <td style="padding:6px 8px;">${escapeHtml(k.created_by)}</td>
            <td style="padding:6px 8px;">${fmtTime(k.created_at)}</td>
            <td style="padding:6px 8px;">${fmtTime(k.last_used_at)}</td>
            <td style="padding:6px 8px;text-align:right;"></td>
        `;
        const del = document.createElement("button");
        del.className = "btn btn-secondary btn-sm";
        del.textContent = "Delete";
        del.addEventListener("click", () => deleteCompanyKey(k));
        tr.lastElementChild.append(del);
        tbody.append(tr);
    }
}

async function deleteCompanyKey(k) {
    if (!confirm(`Delete company key "${k.name}"?\n\nEVERY client in this company still using its token will stop working immediately.`)) return;
    try {
        await api.deleteCompanyKey(k.id);
    } catch (err) {
        alert(err.message);
        return;
    }
    loadCompanyKeys();
}

// ----- Module-load wiring -----

$("#user-pill").addEventListener("click", openSettings);
$("#settings-close").addEventListener("click", closeSettings);
$("#settings-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "settings-backdrop") closeSettings();
});

$("#key-create").addEventListener("click", async () => {
    const name = $("#key-name").value.trim();
    if (!name) { alert("Give the key a name first."); return; }
    let created;
    try {
        created = await api.createKey(name);
    } catch (err) {
        alert(err.message);
        return;
    }
    $("#key-name").value = "";
    $("#key-reveal-token").textContent = created.token;
    // Build copy-paste snippets pinned to the current origin so the user can
    // wire up Claude / Claude Desktop without assembling URLs by hand.
    const mcpUrl = `${window.location.origin}/mcp`;
    const claudeDesktop = JSON.stringify({
        mcpServers: {
            "voitta-rag-enterprise": {
                type: "http",
                url: mcpUrl,
                headers: { Authorization: `Bearer ${created.token}` },
            },
        },
    }, null, 2);
    const cli = `claude mcp add --transport http voitta-rag-enterprise ${mcpUrl} \\\n  --header "Authorization: Bearer ${created.token}"`;
    $("#key-reveal-claude").textContent = claudeDesktop;
    $("#key-reveal-cli").textContent = cli;
    $("#key-reveal").hidden = false;
    // No refetch — the keys.snapshot push re-renders the table via the store.
});

$("#key-reveal-copy").addEventListener("click", async () => {
    const tok = $("#key-reveal-token").textContent;
    try {
        await navigator.clipboard.writeText(tok);
        const btn = $("#key-reveal-copy");
        const prev = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch {
        // Clipboard API can be blocked (insecure context, permissions). Fall
        // back to selecting the token so the user can copy with the keyboard.
        const node = $("#key-reveal-token");
        const range = document.createRange();
        range.selectNodeContents(node);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
    }
});

$("#key-reveal-dismiss").addEventListener("click", () => {
    $("#key-reveal").hidden = true;
    $("#key-reveal-token").textContent = "";
});

// ----- Company key create / reveal wiring -----

$("#company-key-create").addEventListener("click", async () => {
    const name = $("#company-key-name").value.trim();
    if (!name) { alert("Give the key a name first."); return; }
    let created;
    try {
        created = await api.createCompanyKey(name);
    } catch (err) {
        alert(err.message);
        return;
    }
    $("#company-key-name").value = "";
    $("#company-key-reveal-token").textContent = created.token;
    // Snippets show the company-key contract: the shared token PLUS the
    // per-user email header. Use the admin's own email as the example.
    const mcpUrl = `${window.location.origin}/mcp`;
    const email = me.get()?.email || "user@example.com";
    const claudeDesktop = JSON.stringify({
        mcpServers: {
            "voitta-rag-enterprise": {
                type: "http",
                url: mcpUrl,
                headers: {
                    Authorization: `Bearer ${created.token}`,
                    "X-Voitta-User-Email": email,
                },
            },
        },
    }, null, 2);
    const cli = `claude mcp add --transport http voitta-rag-enterprise ${mcpUrl} \\\n  --header "Authorization: Bearer ${created.token}" \\\n  --header "X-Voitta-User-Email: ${email}"`;
    $("#company-key-reveal-claude").textContent = claudeDesktop;
    $("#company-key-reveal-cli").textContent = cli;
    $("#company-key-reveal").hidden = false;
    loadCompanyKeys();
});

$("#company-key-reveal-copy").addEventListener("click", async () => {
    const tok = $("#company-key-reveal-token").textContent;
    try {
        await navigator.clipboard.writeText(tok);
        const btn = $("#company-key-reveal-copy");
        const prev = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch {
        const node = $("#company-key-reveal-token");
        const range = document.createRange();
        range.selectNodeContents(node);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
    }
});

$("#company-key-reveal-dismiss").addEventListener("click", () => {
    $("#company-key-reveal").hidden = true;
    $("#company-key-reveal-token").textContent = "";
});
