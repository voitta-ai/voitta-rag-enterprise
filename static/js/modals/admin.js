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

const $ = (sel) => document.querySelector(sel);

export function openAdmin() {
    $("#admin-backdrop").hidden = false;
    refreshAdmin();
}

export function closeAdmin() {
    $("#admin-backdrop").hidden = true;
}

// Admin modal tabs — Sign-in gate / Users / OAuth providers / Data
// caps / Storage. Pure DOM toggle; refreshAdmin always pulls every
// section regardless of which tab is visible, so flipping tabs is
// instant.
const ADMIN_TABS = ["access", "users", "oauth", "caps", "storage"];

function setAdminTab(name) {
    if (!ADMIN_TABS.includes(name)) name = "access";
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

async function refreshAdmin() {
    try {
        const [allow, users, providers, caps, settings] = await Promise.all([
            api.adminAllowlist(),
            api.adminListUsers(),
            api.adminListAuthProviders(),
            api.adminGetIndexingCaps(),
            api.adminGetSettings(),
        ]);
        renderList("#admin-domains", allow.domains, "domain", api.adminRemoveDomain);
        renderList("#admin-blocked", allow.blocked, "email", api.adminUnblock);
        renderUsersTable(users);
        renderAuthProvidersTable(providers);
        renderCapsTable(caps);
        renderStorageSettings(settings);
    } catch (err) {
        alert(err.message);
    }
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
$("#admin-tab-btn-caps").addEventListener("click", () => setAdminTab("caps"));

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
