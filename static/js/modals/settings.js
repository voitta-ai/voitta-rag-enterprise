// User settings modal — personal API keys + (admins only) company keys.
//
// Each key is shown once at creation; the reveal banner renders the token
// and ready-made Claude / Claude CLI snippets, each with its own copy
// button, so the user can wire up MCP without hand-assembling URLs.
// Standing "how to connect" blocks show the same snippets with a
// placeholder token for keys created earlier (tokens are unrecoverable —
// subsequent renders only show the prefix).
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
    renderMcpHowto();
    // Render from the live store; the WS delivers the user's keys on connect
    // and re-pushes after every create/delete. No HTTP fetch on open.
    renderKeys(keysState.get());
    loadCompanyKeys();
}

// ----- MCP connection snippets -----

const MCP_SERVER_NAME = "voitta-rag-enterprise";

// Claude Desktop JSON + Claude Code CLI for a token. Company keys also
// carry the member's email (X-Voitta-User-Email); personal keys don't —
// pass email only for the company variants.
function mcpSnippets(token, email) {
    const url = `${window.location.origin}/mcp`;
    const headers = { Authorization: `Bearer ${token}` };
    if (email != null) headers["X-Voitta-User-Email"] = email;
    const json = JSON.stringify(
        { mcpServers: { [MCP_SERVER_NAME]: { type: "http", url, headers } } },
        null, 2);
    let cli = `claude mcp add --transport http ${MCP_SERVER_NAME} ${url} \\\n  --header "Authorization: Bearer ${token}"`;
    if (email != null) cli += ` \\\n  --header "X-Voitta-User-Email: ${email}"`;
    return { json, cli };
}

// Standing "how to connect" blocks: the same snippets with a placeholder
// token, so setup instructions are on the page even when no key was just
// created (tokens are unrecoverable after creation).
function renderMcpHowto() {
    const url = `${window.location.origin}/mcp`;
    for (const el of document.querySelectorAll("#settings-backdrop .mcp-endpoint")) {
        el.textContent = url;
    }
    const personal = mcpSnippets("<YOUR_API_KEY>");
    $("#mcp-howto-json").textContent = personal.json;
    $("#mcp-howto-cli").textContent = personal.cli;
    const company = mcpSnippets("<YOUR_COMPANY_KEY>", me.get()?.email || "user@example.com");
    $("#mcp-howto-company-json").textContent = company.json;
    $("#mcp-howto-company-cli").textContent = company.cli;
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
    // Reveal the token + copy-paste snippets pinned to the current origin so
    // the user can wire up Claude / Claude Desktop without assembling URLs.
    $("#key-reveal-token").textContent = created.token;
    const snip = mcpSnippets(created.token);
    $("#key-reveal-claude").textContent = snip.json;
    $("#key-reveal-cli").textContent = snip.cli;
    $("#key-reveal").hidden = false;
    // No refetch — the keys.snapshot push re-renders the table via the store.
});

// One handler for every copy button: data-copy points at the element whose
// textContent gets copied. Clipboard API can be blocked (insecure context,
// permissions) — fall back to selecting the text for a keyboard copy.
async function copySnippet(btn) {
    const src = document.querySelector(btn.dataset.copy);
    if (!src) return;
    try {
        await navigator.clipboard.writeText(src.textContent);
        const prev = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch {
        const range = document.createRange();
        range.selectNodeContents(src);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
    }
}
for (const btn of document.querySelectorAll("#settings-backdrop .copy-snippet")) {
    btn.addEventListener("click", () => copySnippet(btn));
}

$("#key-reveal-dismiss").addEventListener("click", () => {
    $("#key-reveal").hidden = true;
    // The snippets embed the token — clear them along with it.
    for (const sel of ["#key-reveal-token", "#key-reveal-claude", "#key-reveal-cli"]) {
        $(sel).textContent = "";
    }
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
    const snip = mcpSnippets(created.token, me.get()?.email || "user@example.com");
    $("#company-key-reveal-claude").textContent = snip.json;
    $("#company-key-reveal-cli").textContent = snip.cli;
    $("#company-key-reveal").hidden = false;
    loadCompanyKeys();
});

$("#company-key-reveal-dismiss").addEventListener("click", () => {
    $("#company-key-reveal").hidden = true;
    for (const sel of ["#company-key-reveal-token", "#company-key-reveal-claude", "#company-key-reveal-cli"]) {
        $(sel).textContent = "";
    }
});
