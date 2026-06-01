// User settings modal — currently just the personal API keys panel.
//
// Each key is shown once at creation; the modal renders a copy
// affordance + ready-made Claude / Claude CLI snippets so the user
// can wire up MCP without hand-assembling URLs. Subsequent renders
// only show the prefix — there's no recover-the-token path.

import { api } from "../api.js";
import { keysState } from "../store.js";

const $ = (sel) => document.querySelector(sel);

export function openSettings() {
    wireKeysStore();
    $("#settings-backdrop").hidden = false;
    $("#key-reveal").hidden = true;
    $("#key-name").value = "";
    // Render from the live store; the WS delivers the user's keys on connect
    // and re-pushes after every create/delete. No HTTP fetch on open.
    renderKeys(keysState.get());
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
