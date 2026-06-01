// Admin modal — four tabs:
// - Sign-in gate: allowed domains, blocked emails
// - Users: pre-create + admin flag + impersonation
// - OAuth providers: catalog of (label, client_id, client_secret) rows
//   seeded from .env on startup, editable inline.
// - Data file caps: runtime-tunable indexing limits (max file bytes,
//   xlsx rows/cols, ipynb output chars, PDF rendering knobs, …) backed by
//   /admin/indexing_caps.json.
//
// Self-contained: opens itself when the Admin button is clicked, wires
// its own close + tab + form-submit handlers at module load.

import { api } from "../api.js";
import { adminState } from "../store.js";
import { createChipSelect } from "../components/chip_select.js";

const $ = (sel) => document.querySelector(sel);

export function openAdmin() {
    wireAdminStore();
    $("#admin-backdrop").hidden = false;
    // Render immediately from the live store (no HTTP fetch). The WS delivers
    // an ``admin.snapshot`` on connect and re-pushes after every admin
    // mutation, so the store is the single source of truth.
    refreshAdmin();
}

export function closeAdmin() {
    $("#admin-backdrop").hidden = true;
}

// Re-render the (open) admin modal whenever the admin state changes — covers
// the connect snapshot and any mutation, including ones made in another admin's
// tab. Guard against clobbering an input the admin is actively editing: skip
// the re-render while focus is on an input inside the modal; the next push or a
// reopen reconciles. Wired once.
let adminStoreWired = false;
function wireAdminStore() {
    if (adminStoreWired) return;
    adminStoreWired = true;
    adminState.subscribe((state) => {
        if (!state) return;
        const backdrop = $("#admin-backdrop");
        if (backdrop.hidden) return;
        const ae = document.activeElement;
        if (ae && ae.tagName === "INPUT" && backdrop.contains(ae)) return;
        renderAdminFromState(state);
    });
}

// Admin modal tabs — Sign-in gate / Users / OAuth providers / Data
// caps / Storage. Pure DOM toggle; refreshAdmin always pulls every
// section regardless of which tab is visible, so flipping tabs is
// instant.
const ADMIN_TABS = ["access", "users", "groups", "oauth", "caps", "storage"];

function setAdminTab(name) {
    if (!ADMIN_TABS.includes(name)) name = "access";
    // Always land on the users list (not a stuck editor) when (re)entering.
    if (typeof closeUserEditor === "function") closeUserEditor();
    for (const t of ADMIN_TABS) {
        const btn = $(`#admin-tab-btn-${t}`);
        const pane = $(`#admin-tab-pane-${t}`);
        if (!btn || !pane) continue;
        const active = t === name;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-selected", String(active));
        pane.hidden = !active;
    }
}

// Render the whole modal from a state object (the shape build_admin_state()
// produces on the server: { allowlist, users, auth_providers, indexing_caps,
// settings }). ``null`` before the first snapshot — render nothing.
function renderAdminFromState(state) {
    if (!state) return;
    renderList("#admin-domains", state.allowlist.domains, "domain", api.adminRemoveDomain);
    renderList("#admin-blocked", state.allowlist.blocked, "email", api.adminUnblock);
    renderUsersTable(state.users);
    renderGroups(state.groups || [], state.users);
    renderAuthProvidersTable(state.auth_providers);
    renderCapsTable(state.indexing_caps);
    renderStorageSettings(state.settings);
}

// Render the current store value. Mutations no longer refetch — the server
// pushes a fresh ``admin.snapshot`` after each change and the subscription in
// wireAdminStore() re-renders. This just paints whatever's already known.
function refreshAdmin() {
    renderAdminFromState(adminState.get());
}

// ---------------------------------------------------------------------------
// Storage tab (NFS root)
// ---------------------------------------------------------------------------

function renderStorageSettings(settings) {
    const input = $("#admin-nfs-root");
    const status = $("#admin-nfs-status");
    const saveBtn = $("#admin-nfs-save");
    const clearBtn = $("#admin-nfs-clear");
    if (!input || !status || !saveBtn || !clearBtn) return;
    // Reset value to server truth on every refresh, but only when the
    // user isn't actively editing (focus on the input is treated as
    // "leave my draft alone"). Saves the awkward case where a
    // background WS refresh clobbers what they were typing.
    if (document.activeElement !== input) {
        input.value = settings.nfs_root || "";
    }
    paintNfsStatus(status, settings);
    // Bind once.
    if (!saveBtn._bound) {
        saveBtn._bound = true;
        saveBtn.addEventListener("click", async () => {
            try {
                const out = await api.adminUpdateSettings({ nfs_root: input.value.trim() });
                input.value = out.nfs_root;
                paintNfsStatus(status, out);
            } catch (err) {
                paintNfsStatus(status, { nfs_available: false, nfs_status: err.message || "save failed", nfs_root: input.value });
            }
        });
    }
    if (!clearBtn._bound) {
        clearBtn._bound = true;
        clearBtn.addEventListener("click", async () => {
            try {
                const out = await api.adminUpdateSettings({ nfs_root: "" });
                input.value = "";
                paintNfsStatus(status, out);
            } catch (err) {
                alert(err.message);
            }
        });
    }
}

function paintNfsStatus(el, settings) {
    const root = settings.nfs_root || "";
    const status = settings.nfs_status || (root ? "ok" : "disabled");
    const available = !!settings.nfs_available;
    el.hidden = false;
    el.classList.remove("ok", "warn", "err");
    if (!root) {
        el.classList.add("warn");
        el.textContent = "Disabled — folder owners will not see NFS as a sync option.";
        return;
    }
    if (available) {
        el.classList.add("ok");
        el.textContent = `Available — folder owners can now configure NFS sync rooted at ${root}.`;
    } else {
        el.classList.add("err");
        el.textContent = `Unavailable (${status}). Fix the mount or pick another path; users won't see NFS as a sync option.`;
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

    // Tenant ID — only meaningful for Microsoft. Render an editor for
    // Microsoft rows and a muted dash for everything else so the column
    // stays aligned without confusing the admin into typing a tenant
    // for a Google row.
    const tdTenant = document.createElement("td");
    if (p.provider === "microsoft") {
        tdTenant.appendChild(
            buildAuthProviderInput(p.id, "tenant_id", p.tenant_id || "", "Tenant ID")
        );
    } else {
        tdTenant.className = "muted";
        tdTenant.textContent = "—";
    }
    tr.appendChild(tdTenant);

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
    const checkBtn = document.createElement("button");
    checkBtn.className = "btn btn-secondary btn-xs";
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
    delBtn.className = "admin-icon-btn admin-icon-danger";
    delBtn.textContent = "🗑";
    delBtn.title = p.source === "env"
        ? "Delete (will be re-created on next restart while .env still has these values)"
        : "Delete";
    delBtn.style.marginLeft = "6px";
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
    const tenantId = $("#admin-auth-provider-tenant-id").value.trim();
    if (!clientId) { alert("Client ID is required"); return; }
    if (provider === "microsoft" && !tenantId) {
        alert("Tenant ID is required for Microsoft providers");
        return;
    }
    try {
        await api.adminCreateAuthProvider({
            provider,
            label,
            client_id: clientId,
            client_secret: clientSecret,
            tenant_id: tenantId,
            enabled: true,
        });
        $("#admin-auth-provider-label").value = "";
        $("#admin-auth-provider-client-id").value = "";
        $("#admin-auth-provider-client-secret").value = "";
        $("#admin-auth-provider-tenant-id").value = "";
        await refreshAdmin();
    } catch (err) {
        alert(err.message);
    }
}

// ---------------------------------------------------------------------------
// Data file caps
// ---------------------------------------------------------------------------

// Layout/copy for every cap row. Order is the rendering order. Keep this
// in sync with the dataclass on the server — unknown keys returned by
// the API are still rendered below as a fallback so a new field added
// server-side surfaces immediately, just without the curated label.
const CAPS_FIELDS = [
    { key: "max_file_bytes", label: "Max file size (global)",
      hint: "Scanner skips files larger than this. Applies to every extension.",
      type: "bytes" },
    { key: "data_file_max_bytes", label: "Data file size cap",
      hint: "Per-extension cap for .json/.jsonl/.ndjson/.csv/.tsv/.xml/.yaml/.yml. Oversized data files are parked in 'unsupported' instead of being chunked. 0 disables this.",
      type: "bytes" },
    { key: "xlsx_max_rows", label: "XLSX max rows per sheet",
      hint: "Rows beyond this are not rendered into markdown.", type: "int" },
    { key: "xlsx_max_cols", label: "XLSX max columns per sheet",
      hint: "Columns beyond this are dropped from each row.", type: "int" },
    { key: "ipynb_max_output_chars", label: "IPYNB output chars per cell",
      hint: "Each code-cell's text output is truncated to this length.", type: "int" },
    { key: "pdf_pages_per_bucket", label: "PDF pages per bucket",
      hint: "Above this many pages, MinerU runs in bucketed mode.", type: "int" },
    { key: "pdf_parse_timeout_s", label: "PDF parse timeout (s)",
      hint: "Per-bucket wall-clock budget before the MinerU subprocess is killed.",
      type: "int" },
    { key: "pdf_page_render_long_edge_px", label: "PDF page render long-edge (px)",
      hint: "Pixel dimension for per-page WebP layout previews.", type: "int" },
    { key: "pdf_page_render_webp_quality", label: "PDF page render WebP quality",
      hint: "1–100; higher = larger files. 75 is a good balance.", type: "int" },
];

const _BYTES_UNITS = [
    { label: "B", factor: 1 },
    { label: "KB", factor: 1024 },
    { label: "MB", factor: 1024 ** 2 },
    { label: "GB", factor: 1024 ** 3 },
];

function _fmtBytes(n) {
    if (!Number.isFinite(n) || n < 1024) return `${n} B`;
    for (let i = _BYTES_UNITS.length - 1; i >= 0; i--) {
        const u = _BYTES_UNITS[i];
        if (n >= u.factor) {
            const v = n / u.factor;
            // Trim insignificant zeros: 5.00 → 5, 1.50 → 1.5.
            return `${v.toFixed(v < 10 ? 2 : 1).replace(/\.?0+$/, "")} ${u.label}`;
        }
    }
    return `${n} B`;
}

// Accept either a bare integer (bytes) or "<n><unit>" where unit is B/KB/MB/GB.
// Returns NaN for unparseable input. Used for both the value column and the
// range cell so the table renders the same units the user types.
function _parseBytes(s) {
    if (typeof s === "number") return s;
    if (s == null) return NaN;
    const trimmed = String(s).trim();
    if (!trimmed) return NaN;
    const m = trimmed.match(/^([\d.]+)\s*([a-z]*)$/i);
    if (!m) return NaN;
    const v = Number(m[1]);
    if (!Number.isFinite(v)) return NaN;
    const unit = (m[2] || "B").toUpperCase();
    const u = _BYTES_UNITS.find((x) => x.label === unit || (unit === "" && x.label === "B"));
    if (!u) return NaN;
    return Math.round(v * u.factor);
}

function renderCapsTable(caps) {
    const tbody = $("#admin-caps-table tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    const { values, defaults, bounds } = caps;

    // Render the curated list first, then any leftover keys we didn't know
    // about (defensive — server can add a field without a UI deploy).
    const seen = new Set();
    for (const field of CAPS_FIELDS) {
        if (!(field.key in values)) continue;
        tbody.appendChild(buildCapsRow(field, values[field.key], defaults[field.key], bounds[field.key]));
        seen.add(field.key);
    }
    for (const key of Object.keys(values)) {
        if (seen.has(key)) continue;
        tbody.appendChild(buildCapsRow(
            { key, label: key, hint: "", type: "int" },
            values[key], defaults[key], bounds[key],
        ));
    }
}

function buildCapsRow(field, value, defaultValue, bound) {
    const tr = document.createElement("tr");

    // Label + hint stack.
    const tdLabel = document.createElement("td");
    tdLabel.style.maxWidth = "320px";
    const labelDiv = document.createElement("div");
    labelDiv.textContent = field.label;
    labelDiv.style.fontWeight = "500";
    tdLabel.appendChild(labelDiv);
    if (field.hint) {
        const hint = document.createElement("div");
        hint.className = "hint";
        hint.style.marginTop = "2px";
        hint.textContent = field.hint;
        tdLabel.appendChild(hint);
    }
    tr.appendChild(tdLabel);

    // Editable value cell.
    const tdValue = document.createElement("td");
    const input = document.createElement("input");
    input.type = "text";
    input.style.width = "140px";
    input.value = field.type === "bytes" ? _fmtBytes(value) : String(value);
    let original = input.value;
    const commit = async () => {
        if (input.value === original) return;
        const parsed = field.type === "bytes" ? _parseBytes(input.value) : Number(input.value);
        if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < 0) {
            alert(`Invalid value for ${field.label}: ${input.value}`);
            input.value = original;
            return;
        }
        try {
            const out = await api.adminUpdateIndexingCaps({ [field.key]: parsed });
            // Server may clamp — reflect the final value in the input.
            const finalVal = out.values[field.key];
            input.value = field.type === "bytes" ? _fmtBytes(finalVal) : String(finalVal);
            original = input.value;
        } catch (err) {
            input.value = original;
            alert(err.message);
        }
    };
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = original; input.blur(); }
    });
    tdValue.appendChild(input);
    tr.appendChild(tdValue);

    // Default + range cells (read-only, dim).
    const tdDefault = document.createElement("td");
    tdDefault.className = "hint";
    tdDefault.textContent = field.type === "bytes" ? _fmtBytes(defaultValue) : String(defaultValue);
    tr.appendChild(tdDefault);

    const tdRange = document.createElement("td");
    tdRange.className = "hint";
    if (Array.isArray(bound)) {
        const [lo, hi] = bound;
        tdRange.textContent = field.type === "bytes"
            ? `${_fmtBytes(lo)} – ${_fmtBytes(hi)}`
            : `${lo} – ${hi}`;
    }
    tr.appendChild(tdRange);

    // Reset button — pushes the shipped default through the API. Same
    // commit path so any clamping the server might do still applies.
    const tdReset = document.createElement("td");
    const resetBtn = document.createElement("button");
    resetBtn.className = "btn btn-secondary btn-sm";
    resetBtn.textContent = "Reset";
    resetBtn.title = "Restore the shipped default";
    resetBtn.addEventListener("click", async () => {
        try {
            const out = await api.adminUpdateIndexingCaps({ [field.key]: defaultValue });
            const finalVal = out.values[field.key];
            input.value = field.type === "bytes" ? _fmtBytes(finalVal) : String(finalVal);
            original = input.value;
        } catch (err) {
            alert(err.message);
        }
    });
    tdReset.appendChild(resetBtn);
    tr.appendChild(tdReset);

    return tr;
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

let _userFilter = "";

function renderUsersTable(users) {
    const tbody = $("#admin-users-table tbody");
    tbody.innerHTML = "";
    const q = _userFilter.trim().toLowerCase();
    const rows = q
        ? users.filter((u) =>
            u.email.toLowerCase().includes(q) ||
            (u.display_name || "").toLowerCase().includes(q) ||
            (u.groups || []).some((g) => g.toLowerCase().includes(q)))
        : users;
    for (const u of rows) {
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

        const tdGroups = document.createElement("td");
        tdGroups.className = "admin-cell-groups";
        tdGroups.textContent = (u.groups && u.groups.length) ? u.groups.join(", ") : "—";
        tr.appendChild(tdGroups);

        const tdActions = document.createElement("td");
        tdActions.className = "admin-actions-cell";
        const actions = document.createElement("div");
        actions.className = "row-actions";

        const editBtn = document.createElement("button");
        editBtn.className = "btn btn-secondary btn-xs";
        editBtn.textContent = "Edit";
        editBtn.addEventListener("click", () => openUserEditor(u));
        actions.appendChild(editBtn);

        const viewBtn = document.createElement("button");
        viewBtn.className = "btn btn-secondary btn-xs";
        viewBtn.textContent = "View as";
        viewBtn.addEventListener("click", async () => {
            try {
                await api.adminImpersonate(u.id);
                window.location.reload();
            } catch (err) { alert(err.message); }
        });
        actions.appendChild(viewBtn);

        // Delete — compact icon button, hidden for super-admins (backend
        // refuses it anyway).
        if (!u.is_super_admin) {
            const delBtn = document.createElement("button");
            delBtn.className = "admin-icon-btn admin-icon-danger";
            delBtn.textContent = "🗑";
            delBtn.title = "Delete user";
            delBtn.setAttribute("aria-label", `Delete ${u.email}`);
            delBtn.addEventListener("click", async () => {
                if (!confirm(`Delete user ${u.email}?\n\nThis removes their account, API keys, and folder grants. Folders they own become unowned.`)) return;
                try { await api.adminDeleteUser(u.id); }
                catch (err) { alert(err.message); }
            });
            actions.appendChild(delBtn);
        }
        tdActions.appendChild(actions);
        tr.appendChild(tdActions);

        tbody.appendChild(tr);
    }
    const countEl = $("#admin-users-count");
    if (countEl) {
        const shown = rows.length, total = users.length;
        countEl.textContent = shown === total
            ? `${total} user${total === 1 ? "" : "s"}`
            : `${shown} of ${total} users`;
    }
}

// ---------------------------------------------------------------------------
// User editor (slide-over) — add or edit one user
// ---------------------------------------------------------------------------

let _editingUserId = null;   // null = adding
let _ueGroupSelect = null;   // the chip-select instance

function _allGroupNames() {
    const s = adminState.get();
    return (s && s.groups ? s.groups.map((g) => g.name) : []);
}

function openUserEditor(user) {
    _editingUserId = user ? user.id : null;
    $("#admin-user-editor-title").textContent = user ? "Edit user" : "Add user";
    const emailEl = $("#admin-ue-email");
    emailEl.value = user ? user.email : "";
    emailEl.disabled = !!user;  // email is the key — immutable once created
    $("#admin-ue-email-hint").hidden = !user;
    $("#admin-ue-name").value = user ? (user.display_name || "") : "";
    $("#admin-ue-admin").checked = user ? user.is_admin : false;
    $("#admin-ue-admin").disabled = user ? user.is_super_admin : false;
    // "Allow sign-in" only applies when creating (it adds to the allowlist).
    $("#admin-ue-signin-row").hidden = !!user;
    $("#admin-ue-signin").checked = true;
    $("#admin-ue-error").hidden = true;

    const host = $("#admin-ue-groups");
    host.innerHTML = "";
    _ueGroupSelect = createChipSelect({
        selected: user ? (user.groups || []) : [],
        options: _allGroupNames,
        allowCreate: true,
        placeholder: "add or create a group…",
    });
    host.appendChild(_ueGroupSelect.el);

    // Full-pane swap: hide the list, show the editor.
    $("#admin-users-list-view").hidden = true;
    $("#admin-user-editor").hidden = false;
    (user ? $("#admin-ue-name") : emailEl).focus();
}

function closeUserEditor() {
    $("#admin-user-editor").hidden = true;
    $("#admin-users-list-view").hidden = false;
    _editingUserId = null;
    _ueGroupSelect = null;
}

async function saveUserEditor() {
    const errEl = $("#admin-ue-error");
    errEl.hidden = true;
    const groups = _ueGroupSelect ? _ueGroupSelect.getValues() : [];
    const name = $("#admin-ue-name").value.trim();
    const isAdmin = $("#admin-ue-admin").checked;
    try {
        let userId = _editingUserId;
        if (userId === null) {
            const email = $("#admin-ue-email").value.trim().toLowerCase();
            if (!email) { errEl.textContent = "Email is required."; errEl.hidden = false; return; }
            const created = await api.adminCreateUser(email, isAdmin);
            userId = created.id;
        }
        // PATCH carries name + groups (+ admin for existing users; for new the
        // create call already set admin, but re-sending is harmless).
        await api.adminUpdateUser(userId, {
            is_admin: isAdmin,
            display_name: name,
            groups,
        });
        closeUserEditor();
    } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
    }
}

// ---------------------------------------------------------------------------
// Groups tab
// ---------------------------------------------------------------------------

let _selectedGroupId = null;
let _gdMemberSelect = null;

function renderGroups(groups, users) {
    const list = $("#admin-groups-list");
    if (!list) return;
    list.innerHTML = "";
    for (const g of groups) {
        const li = document.createElement("li");
        li.className = "admin-group-row" + (g.id === _selectedGroupId ? " selected" : "");
        const name = document.createElement("span");
        name.className = "admin-group-name";
        name.textContent = g.name;
        const count = document.createElement("span");
        count.className = "admin-group-count";
        count.textContent = `${g.member_count} member${g.member_count === 1 ? "" : "s"}`;
        li.append(name, count);
        li.addEventListener("click", () => { _selectedGroupId = g.id; renderAdminFromState(adminState.get()); });
        list.appendChild(li);
    }
    // If a group is selected, (re)render its detail panel from fresh state.
    const sel = groups.find((g) => g.id === _selectedGroupId);
    if (sel) renderGroupDetail(sel, users);
    else $("#admin-group-detail").hidden = true;
}

function renderGroupDetail(group, users) {
    $("#admin-group-detail").hidden = false;
    $("#admin-group-detail-title").textContent = `Group: ${group.name}`;
    const nameEl = $("#admin-gd-name");
    const descEl = $("#admin-gd-desc");
    if (document.activeElement !== nameEl) nameEl.value = group.name;
    if (document.activeElement !== descEl) descEl.value = group.description || "";

    // Members = users whose groups include this group's name.
    const members = users.filter((u) => (u.groups || []).includes(group.name));
    $("#admin-gd-members-label").textContent = `Members (${members.length})`;
    const ul = $("#admin-gd-members");
    ul.innerHTML = "";
    for (const u of members) {
        const li = document.createElement("li");
        li.className = "admin-list-row";
        const label = document.createElement("span");
        label.textContent = u.display_name ? `${u.display_name} <${u.email}>` : u.email;
        const x = document.createElement("button");
        x.className = "btn btn-secondary btn-sm";
        x.textContent = "✕";
        x.title = "Remove from group";
        x.addEventListener("click", async () => {
            try { await api.adminRemoveGroupMember(group.id, u.id); }
            catch (err) { alert(err.message); }
        });
        li.append(label, x);
        ul.appendChild(li);
    }

    // Add-member picker: users not already in the group.
    const host = $("#admin-gd-addmember");
    host.innerHTML = "";
    const nonMembers = users.filter((u) => !(u.groups || []).includes(group.name));
    const byLabel = new Map(nonMembers.map((u) => [u.email, u.id]));
    _gdMemberSelect = createChipSelect({
        selected: [],
        options: () => [...byLabel.keys()],
        allowCreate: false,
        placeholder: "add a user by email…",
        onChange: async (vals) => {
            // Single-add semantics: when a value is picked, add and clear.
            const email = vals[vals.length - 1];
            const uid = byLabel.get(email);
            if (uid == null) return;
            // Drop focus first so the incoming admin.snapshot push isn't
            // skipped by the editing focus-guard in wireAdminStore — that's
            // what refreshes the member list after the add.
            if (document.activeElement) document.activeElement.blur();
            _gdMemberSelect.setValues([]);
            try { await api.adminAddGroupMember(group.id, uid); }
            catch (err) { alert(err.message); }
        },
    });
    host.appendChild(_gdMemberSelect.el);
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

// ---------------------------------------------------------------------------
// Module-load wiring
// ---------------------------------------------------------------------------

for (const t of ADMIN_TABS) {
    $(`#admin-tab-btn-${t}`).addEventListener("click", () => setAdminTab(t));
}

$("#admin-auth-provider-add").addEventListener("click", submitAddAuthProvider);
$("#admin-auth-provider-client-secret").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitAddAuthProvider(); }
});

// Tenant ID is Microsoft-only. Toggle visibility + retune the
// client_id/secret placeholders so the form reads correctly for each
// provider.
function updateAuthProviderTypeUi() {
    const provider = $("#admin-auth-provider-type").value;
    const tenantRow = $("#admin-auth-provider-tenant-row");
    if (tenantRow) tenantRow.hidden = provider !== "microsoft";
    const cid = $("#admin-auth-provider-client-id");
    const secret = $("#admin-auth-provider-client-secret");
    if (provider === "microsoft") {
        cid.placeholder = "00000000-0000-0000-0000-000000000000";
        secret.placeholder = "(paste Azure AD app secret)";
    } else if (provider === "github") {
        cid.placeholder = "Iv1.…";
        secret.placeholder = "(paste GitHub app secret)";
    } else {
        cid.placeholder = "123…apps.googleusercontent.com";
        secret.placeholder = "GOCSPX-…";
    }
}
$("#admin-auth-provider-type").addEventListener("change", updateAuthProviderTypeUi);
updateAuthProviderTypeUi();

$("#btn-admin").addEventListener("click", openAdmin);
$("#admin-close").addEventListener("click", closeAdmin);
$("#admin-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "admin-backdrop") closeAdmin();
});

wireAdminAdd("#admin-domain-input", "#admin-domain-add", api.adminAddDomain);
wireAdminAdd("#admin-block-input", "#admin-block-add", api.adminBlock);

// Users tab: filter + add + slide-over editor.
$("#admin-user-filter").addEventListener("input", (e) => {
    _userFilter = e.target.value;
    renderUsersTable(adminState.get()?.users || []);
});
$("#admin-user-add-btn").addEventListener("click", () => openUserEditor(null));
$("#admin-ue-cancel").addEventListener("click", closeUserEditor);
$("#admin-ue-back").addEventListener("click", closeUserEditor);
$("#admin-ue-save").addEventListener("click", saveUserEditor);

// Groups tab: create + detail name/desc save + delete.
async function submitNewGroup() {
    const input = $("#admin-group-new-name");
    const name = input.value.trim();
    if (!name) return;
    try {
        const g = await api.adminCreateGroup(name, "");
        input.value = "";
        _selectedGroupId = g.id;  // select the just-created group
    } catch (err) { alert(err.message); }
}
$("#admin-group-add-btn").addEventListener("click", submitNewGroup);
$("#admin-group-new-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitNewGroup(); }
});

async function saveGroupMeta() {
    if (_selectedGroupId == null) return;
    const name = $("#admin-gd-name").value.trim();
    const description = $("#admin-gd-desc").value.trim();
    if (!name) return;
    try { await api.adminUpdateGroup(_selectedGroupId, { name, description }); }
    catch (err) { alert(err.message); }
}
// Commit name/description on blur (focus-guard in wireAdminStore keeps live
// pushes from clobbering the field while it's focused).
$("#admin-gd-name").addEventListener("blur", saveGroupMeta);
$("#admin-gd-desc").addEventListener("blur", saveGroupMeta);

$("#admin-group-delete").addEventListener("click", async () => {
    if (_selectedGroupId == null) return;
    const g = (adminState.get()?.groups || []).find((x) => x.id === _selectedGroupId);
    if (!g) return;
    if (!confirm(`Delete group "${g.name}"?\n\nMembers are not deleted — they just lose this group.`)) return;
    try {
        await api.adminDeleteGroup(_selectedGroupId);
        _selectedGroupId = null;
    } catch (err) { alert(err.message); }
});

$("#btn-stop-impersonate").addEventListener("click", async () => {
    try {
        await api.adminStopImpersonate();
        window.location.reload();
    } catch (err) { alert(err.message); }
});
