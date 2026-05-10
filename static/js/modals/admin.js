// Admin modal — three tabs:
// - Sign-in gate: allowed domains, blocked emails
// - Users: pre-create + admin flag + impersonation
// - OAuth providers: catalog of (label, client_id, client_secret) rows
//   seeded from .env on startup, editable inline.
//
// Self-contained: opens itself when the Admin button is clicked, wires
// its own close + tab + form-submit handlers at module load.

import { api } from "../api.js";

const $ = (sel) => document.querySelector(sel);

export function openAdmin() {
    $("#admin-backdrop").hidden = false;
    refreshAdmin();
}

export function closeAdmin() {
    $("#admin-backdrop").hidden = true;
}

// Admin modal tabs — Sign-in gate / Users / OAuth providers. Pure DOM
// toggle; refreshAdmin always pulls all three sections regardless of
// which tab is visible, so flipping tabs is instant.
function setAdminTab(name) {
    if (!["access", "users", "oauth"].includes(name)) name = "access";
    for (const t of ["access", "users", "oauth"]) {
        const btn = $(`#admin-tab-btn-${t}`);
        const pane = $(`#admin-tab-pane-${t}`);
        if (!btn || !pane) continue;
        const active = t === name;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-selected", String(active));
        pane.hidden = !active;
    }
}

async function refreshAdmin() {
    try {
        const [allow, users, providers] = await Promise.all([
            api.adminAllowlist(),
            api.adminListUsers(),
            api.adminListAuthProviders(),
        ]);
        renderList("#admin-domains", allow.domains, "domain", api.adminRemoveDomain);
        renderList("#admin-blocked", allow.blocked, "email", api.adminUnblock);
        renderUsersTable(users);
        renderAuthProvidersTable(providers);
    } catch (err) {
        alert(err.message);
    }
}

// ---------------------------------------------------------------------------
// OAuth providers
// ---------------------------------------------------------------------------

function renderAuthProvidersTable(providers) {
    const tbody = $("#admin-auth-providers-table tbody");
    const empty = $("#admin-auth-providers-empty");
    tbody.innerHTML = "";
    if (!providers.length) {
        empty.hidden = false;
        return;
    }
    empty.hidden = true;
    for (const p of providers) {
        tbody.appendChild(buildAuthProviderRow(p));
    }
}

// One row in the auth providers table. Inputs are live-editable; changes
// fire PATCH requests on blur (or Enter), so the admin can correct a
// pasted client_id without an explicit "save" click. The Check button
// rolls a credential probe through the backend; a small status pill
// appears next to it for ~5s.
function buildAuthProviderRow(p) {
    const tr = document.createElement("tr");
    tr.dataset.providerId = String(p.id);

    // Provider name (read-only after creation; switching providers would
    // be a different OAuth flow entirely).
    const tdProvider = document.createElement("td");
    tdProvider.textContent = p.provider;
    if (p.source === "env") {
        const badge = document.createElement("span");
        badge.className = "badge-super";
        badge.style.background = "#3b82f6";
        badge.textContent = ".env";
        badge.title = "Seeded from .env on startup. Deleting this row only sticks until the next restart while the env vars remain set.";
        tdProvider.appendChild(badge);
    }
    tr.appendChild(tdProvider);

    // Label, client_id, client_secret — inline editors.
    const tdLabel = document.createElement("td");
    tdLabel.appendChild(buildAuthProviderInput(p.id, "label", p.label, "Label"));
    tr.appendChild(tdLabel);

    const tdClientId = document.createElement("td");
    tdClientId.appendChild(buildAuthProviderInput(p.id, "client_id", p.client_id, "Client ID"));
    tr.appendChild(tdClientId);

    const tdSecret = document.createElement("td");
    tdSecret.appendChild(buildAuthProviderInput(p.id, "client_secret", p.client_secret, "Client secret"));
    tr.appendChild(tdSecret);

    // Enabled toggle.
    const tdEnabled = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!p.enabled;
    cb.title = p.enabled ? "Disable" : "Enable";
    cb.addEventListener("change", async () => {
        try {
            await api.adminUpdateAuthProvider(p.id, { enabled: cb.checked });
        } catch (err) {
            cb.checked = !cb.checked;
            alert(err.message);
        }
    });
    tdEnabled.appendChild(cb);
    tr.appendChild(tdEnabled);

    // Actions: Check + Delete.
    const tdActions = document.createElement("td");
    tdActions.style.whiteSpace = "nowrap";
    const checkBtn = document.createElement("button");
    checkBtn.className = "btn btn-secondary btn-sm";
    checkBtn.textContent = "Check";
    checkBtn.title = "Probe the provider's token endpoint to verify these credentials";
    const checkStatus = document.createElement("span");
    checkStatus.className = "hint";
    checkStatus.style.marginLeft = "8px";
    checkBtn.addEventListener("click", async () => {
        checkBtn.disabled = true;
        checkStatus.textContent = "Checking…";
        checkStatus.style.color = "";
        try {
            const r = await api.adminCheckAuthProvider(p.id);
            checkStatus.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
            checkStatus.style.color = r.ok ? "#10b981" : "#dc3545";
        } catch (err) {
            checkStatus.textContent = "✗ " + (err.message || "request failed");
            checkStatus.style.color = "#dc3545";
        } finally {
            checkBtn.disabled = false;
            // Auto-clear after a beat so the row doesn't stay loud.
            setTimeout(() => { checkStatus.textContent = ""; }, 8000);
        }
    });
    const delBtn = document.createElement("button");
    delBtn.className = "btn-remove";
    delBtn.textContent = "×";
    delBtn.title = p.source === "env"
        ? "Delete (will be re-created on next restart while .env still has these values)"
        : "Delete";
    delBtn.style.marginLeft = "8px";
    delBtn.addEventListener("click", async () => {
        if (!confirm(`Delete ${p.provider} provider "${p.label || p.client_id}"?`)) return;
        try {
            await api.adminDeleteAuthProvider(p.id);
            await refreshAdmin();
        } catch (err) {
            alert(err.message);
        }
    });
    tdActions.append(checkBtn, checkStatus, delBtn);
    tr.appendChild(tdActions);

    return tr;
}

function buildAuthProviderInput(providerId, field, value, placeholder) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.placeholder = placeholder;
    input.style.width = "100%";
    input.style.minWidth = field === "client_id" ? "240px" : "120px";
    let original = value || "";
    const commit = async () => {
        if (input.value === original) return;
        try {
            await api.adminUpdateAuthProvider(providerId, { [field]: input.value });
            original = input.value;
        } catch (err) {
            input.value = original; // revert
            alert(err.message);
        }
    };
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = original; input.blur(); }
    });
    return input;
}

async function submitAddAuthProvider() {
    const provider = $("#admin-auth-provider-type").value;
    const label = $("#admin-auth-provider-label").value.trim();
    const clientId = $("#admin-auth-provider-client-id").value.trim();
    const clientSecret = $("#admin-auth-provider-client-secret").value;
    if (!clientId) { alert("Client ID is required"); return; }
    try {
        await api.adminCreateAuthProvider({
            provider,
            label,
            client_id: clientId,
            client_secret: clientSecret,
            enabled: true,
        });
        $("#admin-auth-provider-label").value = "";
        $("#admin-auth-provider-client-id").value = "";
        $("#admin-auth-provider-client-secret").value = "";
        await refreshAdmin();
    } catch (err) {
        alert(err.message);
    }
}

// ---------------------------------------------------------------------------
// Allowlist / blocklist (renderList) and Users
// ---------------------------------------------------------------------------

function renderList(sel, items, _kind, removeFn) {
    const ul = $(sel);
    ul.innerHTML = "";
    if (!items.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = "(none)";
        ul.appendChild(li);
        return;
    }
    for (const v of items) {
        const li = document.createElement("li");
        const span = document.createElement("span");
        span.textContent = v;
        const btn = document.createElement("button");
        btn.className = "btn-remove";
        btn.title = "Remove";
        btn.textContent = "×";
        btn.addEventListener("click", async () => {
            try { await removeFn(v); await refreshAdmin(); }
            catch (err) { alert(err.message); }
        });
        li.appendChild(span);
        li.appendChild(btn);
        ul.appendChild(li);
    }
}

function renderUsersTable(users) {
    const tbody = $("#admin-users-table tbody");
    tbody.innerHTML = "";
    for (const u of users) {
        const tr = document.createElement("tr");

        const tdEmail = document.createElement("td");
        tdEmail.textContent = u.email;
        if (u.is_super_admin) {
            const badge = document.createElement("span");
            badge.className = "badge-super";
            badge.textContent = "SUPER";
            badge.title = "From VOITTA_SUPER_ADMINS — can't be demoted via UI.";
            tdEmail.appendChild(badge);
        }
        tr.appendChild(tdEmail);

        const tdName = document.createElement("td");
        tdName.textContent = u.display_name || "—";
        tr.appendChild(tdName);

        const tdAdmin = document.createElement("td");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = u.is_admin;
        cb.disabled = u.is_super_admin;
        cb.addEventListener("change", async () => {
            try { await api.adminSetIsAdmin(u.id, cb.checked); }
            catch (err) { alert(err.message); cb.checked = !cb.checked; }
        });
        tdAdmin.appendChild(cb);
        tr.appendChild(tdAdmin);

        const tdActions = document.createElement("td");
        tdActions.className = "row-actions";
        const viewBtn = document.createElement("button");
        viewBtn.className = "btn btn-secondary btn-sm";
        viewBtn.textContent = "View as";
        viewBtn.addEventListener("click", async () => {
            try {
                await api.adminImpersonate(u.id);
                window.location.reload();
            } catch (err) { alert(err.message); }
        });
        tdActions.appendChild(viewBtn);
        tr.appendChild(tdActions);

        tbody.appendChild(tr);
    }
}

// Wire each input + button pair so click and Enter both submit. Without
// the Enter binding the form looked broken to anyone who typed and hit
// return — a real bug report from the first admin-UI session.
function wireAdminAdd(inputSel, buttonSel, apiFn) {
    const input = $(inputSel);
    const submit = async () => {
        const v = input.value.trim();
        if (!v) return;
        try {
            await apiFn(v);
            input.value = "";
            await refreshAdmin();
        } catch (err) {
            alert(err.message);
        }
    };
    $(buttonSel).addEventListener("click", submit);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            submit();
        }
    });
}

// Pre-create user (with optional admin grant). Different shape from the
// allowlist add rows because it has an extra "Admin" checkbox alongside
// the email input — wireAdminAdd only handles single-input.
async function submitAddUser() {
    const input = $("#admin-newuser-input");
    const adminCb = $("#admin-newuser-admin");
    const email = input.value.trim();
    if (!email) return;
    try {
        await api.adminCreateUser(email, adminCb.checked);
        input.value = "";
        adminCb.checked = false;
        await refreshAdmin();
    } catch (err) {
        alert(err.message);
    }
}

// ---------------------------------------------------------------------------
// Module-load wiring
// ---------------------------------------------------------------------------

$("#admin-tab-btn-access").addEventListener("click", () => setAdminTab("access"));
$("#admin-tab-btn-users").addEventListener("click", () => setAdminTab("users"));
$("#admin-tab-btn-oauth").addEventListener("click", () => setAdminTab("oauth"));

$("#admin-auth-provider-add").addEventListener("click", submitAddAuthProvider);
$("#admin-auth-provider-client-secret").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitAddAuthProvider(); }
});

$("#btn-admin").addEventListener("click", openAdmin);
$("#admin-close").addEventListener("click", closeAdmin);
$("#admin-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "admin-backdrop") closeAdmin();
});

wireAdminAdd("#admin-domain-input", "#admin-domain-add", api.adminAddDomain);
wireAdminAdd("#admin-block-input", "#admin-block-add", api.adminBlock);

$("#admin-newuser-add").addEventListener("click", submitAddUser);
$("#admin-newuser-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitAddUser(); }
});

$("#btn-stop-impersonate").addEventListener("click", async () => {
    try {
        await api.adminStopImpersonate();
        window.location.reload();
    } catch (err) { alert(err.message); }
});
