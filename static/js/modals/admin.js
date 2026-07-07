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
import { adminState, me as meStore } from "../store.js";
import { createChipSelect } from "../components/chip_select.js";
import { sourceBadges } from "../components/badges.js";

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
const ADMIN_TABS = [
    "access", "users", "groups", "clerk-users", "clerk-companies",
    "oauth", "caps", "storage",
];

function setAdminTab(name) {
    if (!ADMIN_TABS.includes(name)) name = "access";
    // Directory tabs come and go with the toggles on the Sign-in gate tab.
    // Landing on a hidden one (stale click, toggle flipped under us) falls
    // back to the gate tab.
    const targetBtn = $(`#admin-tab-btn-${name}`);
    if (targetBtn && targetBtn.hidden) name = "access";
    // Always land on the list views (not a stuck editor) when (re)entering.
    if (typeof closeUserEditor === "function") closeUserEditor();
    if (typeof closeGroupEditor === "function") closeGroupEditor();
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
    renderDirectorySettings(state.settings);
    applyDirectoryModes(state.settings);
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
// Directory sources (native + Clerk)
//
// Display-only: the Sign-in gate tab hosts two independent toggles. "Native
// directory" shows/hides the local Users + Groups tabs; "Clerk directory"
// (toggle + secret key) shows/hides the read-only Clerk users + Clerk
// companies tabs. Any combination can be on. Sign-in and authorization are
// untouched.
// ---------------------------------------------------------------------------

let _clerkOn = false;
let _nativeOn = true;
let _clerkDir = null;          // cached /admin/clerk/directory payload
let _clerkDirPromise = null;   // in-flight guard
let _clerkUserFilter = "";

function renderDirectorySettings(settings) {
    const nativeCb = $("#admin-native-enabled");
    const nativeStatus = $("#admin-native-status");
    const cb = $("#admin-clerk-enabled");
    const key = $("#admin-clerk-key");
    const status = $("#admin-clerk-status");
    if (!cb || !key || !status || !nativeCb) return;
    nativeCb.checked = settings.native_directory_enabled !== false;
    if (nativeStatus) {
        nativeStatus.textContent = nativeCb.checked
            ? "" : "Hidden — the Users and Groups tabs are not shown.";
    }
    cb.checked = !!settings.clerk_enabled;
    if (document.activeElement !== key) {
        key.value = settings.clerk_secret_key || "";
    }
    if (settings.clerk_enabled) {
        status.textContent = settings.clerk_key_from_env
            ? "Enabled — key pre-filled from .env (CLERK_SECRET_KEY)."
            : "Enabled.";
    } else {
        status.textContent = settings.clerk_secret_key
            ? (settings.clerk_key_from_env
                ? "Disabled — key pre-filled from .env, toggle on to use it."
                : "Disabled.")
            : "Disabled — paste your Clerk secret key to get started.";
    }
}

async function saveNativeSettings(enabled) {
    const cb = $("#admin-native-enabled");
    if (document.activeElement) document.activeElement.blur();
    try {
        const out = await api.adminUpdateSettings({ native_directory_enabled: enabled });
        renderDirectorySettings(out);
        applyDirectoryModes(out);
    } catch (err) {
        cb.checked = _nativeOn;  // revert the optimistic flip
        alert(err.message || "save failed");
    }
}

async function saveClerkSettings(enabled) {
    const key = $("#admin-clerk-key");
    const cb = $("#admin-clerk-enabled");
    const status = $("#admin-clerk-status");
    // Blur so the WS snapshot push isn't skipped by the focus guard.
    if (document.activeElement) document.activeElement.blur();
    status.textContent = "Saving…";
    try {
        const out = await api.adminUpdateSettings({
            clerk_enabled: enabled,
            clerk_secret_key: key.value.trim(),
        });
        _clerkDir = null;  // key/mode changed — refetch on next render
        renderDirectorySettings(out);
        applyDirectoryModes(out);
    } catch (err) {
        cb.checked = _clerkOn;  // revert the optimistic flip
        status.textContent = "✗ " + (err.message || "save failed");
    }
}

// Show/hide the four directory tabs to match the toggles, and kick off the
// directory fetch when Clerk mode is on.
function applyDirectoryModes(settings) {
    _nativeOn = !!(settings && settings.native_directory_enabled !== false);
    _clerkOn = !!(settings && settings.clerk_enabled);

    const vis = {
        "users": _nativeOn,
        "groups": _nativeOn,
        "clerk-users": _clerkOn,
        "clerk-companies": _clerkOn,
    };
    let activeHidden = false;
    for (const [tab, show] of Object.entries(vis)) {
        const btn = $(`#admin-tab-btn-${tab}`);
        if (!btn) continue;
        btn.hidden = !show;
        if (!show && btn.classList.contains("active")) activeHidden = true;
    }
    // The active tab just disappeared — bounce to the gate tab.
    if (activeHidden) setAdminTab("access");

    if (_clerkOn) loadClerkDirectory();
}

function loadClerkDirectory() {
    if (_clerkDir) { renderClerkDirectory(); return; }
    if (_clerkDirPromise) return;
    setClerkDirStatus("Loading directory from Clerk…");
    _clerkDirPromise = api.adminClerkDirectory()
        .then((dir) => { _clerkDir = dir; renderClerkDirectory(); })
        .catch((err) => setClerkDirStatus("✗ " + (err.message || "Clerk fetch failed")))
        .finally(() => { _clerkDirPromise = null; });
}

function setClerkDirStatus(msg) {
    for (const sel of ["#admin-clerk-users-status", "#admin-clerk-companies-status"]) {
        const el = $(sel);
        if (el) el.textContent = msg;
    }
}

function _fmtClerkDate(ms) {
    if (!ms) return "—";
    try { return new Date(ms).toLocaleDateString(); } catch { return "—"; }
}

function renderClerkDirectory() {
    if (!_clerkDir) return;
    setClerkDirStatus("");
    renderClerkUsersTable();
    renderClerkCompanies();
}

// Super-admin only: "View as" a Clerk user (accounts are provisioned on
// the fly server-side; company_id picks the scope the view lands in).
function _clerkViewAsBtn(email, companyId) {
    const btn = document.createElement("button");
    btn.className = "btn btn-secondary btn-xs";
    btn.textContent = "View as";
    btn.title = companyId
        ? "Impersonate this user in this company's scope"
        : "Impersonate this user (Personal account)";
    btn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();  // don't toggle the surrounding <details>
        try {
            await api.adminClerkImpersonate(email, companyId);
            window.location.reload();
        } catch (err) { alert(err.message); }
    });
    return btn;
}

function _isSuperAdmin() {
    return !!meStore.get()?.is_super_admin;
}

function renderClerkUsersTable() {
    const tbody = $("#admin-clerk-users-table tbody");
    if (!tbody || !_clerkDir) return;
    tbody.innerHTML = "";
    const users = _clerkDir.users || [];
    const superAdmin = _isSuperAdmin();
    const q = _clerkUserFilter.trim().toLowerCase();
    const rows = q
        ? users.filter((u) =>
            u.email.toLowerCase().includes(q) ||
            (u.name || "").toLowerCase().includes(q) ||
            (u.org_names || []).some((n) => n.toLowerCase().includes(q)))
        : users;
    for (const u of rows) {
        const tr = document.createElement("tr");
        const td = (text) => {
            const el = document.createElement("td");
            el.textContent = text;
            return el;
        };
        tr.append(
            td(u.email || "—"),
            td(u.name || "—"),
            td((u.org_names || []).join(", ") || "—"),
            td(_fmtClerkDate(u.last_sign_in_at)),
        );
        const tdActions = document.createElement("td");
        tdActions.className = "admin-actions-cell";
        if (superAdmin && u.email) {
            tdActions.appendChild(_clerkViewAsBtn(u.email, ""));
        }
        tr.appendChild(tdActions);
        tbody.appendChild(tr);
    }
    const countEl = $("#admin-clerk-users-count");
    if (countEl) {
        countEl.textContent = rows.length === users.length
            ? `${users.length} user${users.length === 1 ? "" : "s"} (from Clerk)`
            : `${rows.length} of ${users.length} users (from Clerk)`;
    }
}

// One COLLAPSED card per organization: name + admin email(s) + member
// count in the summary row; the read-only member table (role column
// mirrors Clerk's org:admin/member) renders only when expanded.
function renderClerkCompanies() {
    const host = $("#admin-clerk-companies");
    if (!host || !_clerkDir) return;
    host.innerHTML = "";
    const orgs = _clerkDir.organizations || [];
    if (!orgs.length) {
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = "No organizations in this Clerk instance.";
        host.appendChild(p);
        return;
    }
    for (const org of orgs) {
        const members = org.members || [];
        const card = document.createElement("details");
        card.className = "admin-provider-card admin-clerk-org";

        const head = document.createElement("summary");
        head.className = "admin-clerk-org-head";
        const title = document.createElement("span");
        title.className = "provider-card-title";
        title.textContent = org.name;
        const admins = members.filter((m) => m.role === "admin");
        const adminEl = document.createElement("span");
        adminEl.className = "hint admin-clerk-org-admin";
        adminEl.textContent = admins.length
            ? admins.map((a) => a.email || a.name).filter(Boolean).join(", ")
            : "";
        if (adminEl.textContent) adminEl.title = "Organization admin";
        const count = document.createElement("span");
        count.className = "hint admin-clerk-org-count";
        count.textContent = `${members.length} member${members.length === 1 ? "" : "s"}`;
        head.append(title, adminEl, count);
        card.appendChild(head);

        const superAdmin = _isSuperAdmin();
        const table = document.createElement("table");
        table.className = "admin-table";
        const thead = document.createElement("thead");
        const hr = document.createElement("tr");
        for (const h of ["Email", "Name", "Role", ""]) {
            const th = document.createElement("th");
            th.textContent = h;
            hr.appendChild(th);
        }
        thead.appendChild(hr);
        const tbody = document.createElement("tbody");
        for (const m of members) {
            const tr = document.createElement("tr");
            const td = (text) => {
                const el = document.createElement("td");
                el.textContent = text;
                return el;
            };
            const roleTd = td(m.role || "member");
            if (m.role === "admin") {
                roleTd.textContent = "";
                const badge = document.createElement("span");
                badge.className = "badge-super";
                badge.textContent = "ADMIN";
                roleTd.appendChild(badge);
            }
            const actTd = document.createElement("td");
            actTd.className = "admin-actions-cell";
            if (superAdmin && m.email) {
                // Lands the impersonation in THIS company's account scope.
                actTd.appendChild(_clerkViewAsBtn(m.email, org.id));
            }
            tr.append(td(m.email || "—"), td(m.name || "—"), roleTd, actTd);
            tbody.appendChild(tr);
        }
        table.append(thead, tbody);
        card.appendChild(table);
        host.appendChild(card);
    }
}

// ---------------------------------------------------------------------------
// OAuth providers
// ---------------------------------------------------------------------------

function renderAuthProvidersTable(providers) {
    const list = $("#admin-auth-providers-list");
    const empty = $("#admin-auth-providers-empty");
    list.innerHTML = "";
    if (!providers.length) {
        empty.hidden = false;
        return;
    }
    empty.hidden = true;
    for (const p of providers) {
        list.appendChild(buildAuthProviderCard(p));
    }
}

// One provider as a card (not a table row): a header with the provider
// name + enabled toggle + Check/Delete actions, and a labeled field grid
// below. Cards align cleanly and scale, where a 7-column table of inline
// inputs did not. Field inputs are live-editable — changes PATCH on blur/Enter.
function buildAuthProviderCard(p) {
    const card = document.createElement("div");
    card.className = "admin-provider-card";
    card.dataset.providerId = String(p.id);

    // ----- header: title + enabled + actions -----
    const head = document.createElement("div");
    head.className = "provider-card-head";

    const title = document.createElement("span");
    title.className = "provider-card-title";
    title.textContent = p.provider;
    if (p.source === "env") {
        const badge = document.createElement("span");
        badge.className = "badge-super provider-env-badge";
        badge.textContent = ".env";
        badge.title = "Seeded from .env on startup. Deleting only sticks until the next restart while the env vars remain set.";
        title.appendChild(badge);
    }

    const enabledLabel = document.createElement("label");
    enabledLabel.className = "admin-inline-check provider-enabled";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!p.enabled;
    cb.addEventListener("change", async () => {
        try { await api.adminUpdateAuthProvider(p.id, { enabled: cb.checked }); }
        catch (err) { cb.checked = !cb.checked; alert(err.message); }
    });
    enabledLabel.append(cb, document.createTextNode("Enabled"));

    const checkBtn = document.createElement("button");
    checkBtn.className = "btn btn-secondary btn-xs";
    checkBtn.textContent = "Check";
    checkBtn.title = "Probe the provider's token endpoint to verify these credentials";
    const checkStatus = document.createElement("span");
    checkStatus.className = "hint provider-check-status";
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
            setTimeout(() => { checkStatus.textContent = ""; }, 8000);
        }
    });

    const delBtn = document.createElement("button");
    delBtn.className = "admin-icon-btn admin-icon-danger";
    delBtn.textContent = "🗑";
    delBtn.title = p.source === "env"
        ? "Delete (will be re-created on next restart while .env still has these values)"
        : "Delete";
    delBtn.addEventListener("click", async () => {
        if (!confirm(`Delete ${p.provider} provider "${p.label || p.client_id}"?`)) return;
        try { await api.adminDeleteAuthProvider(p.id); await refreshAdmin(); }
        catch (err) { alert(err.message); }
    });

    const actions = document.createElement("div");
    actions.className = "provider-card-actions";
    actions.append(enabledLabel, checkBtn, delBtn);
    head.append(title, actions);

    // ----- body: labeled field grid (same shape as the add-provider card) -----
    const grid = document.createElement("div");
    grid.className = "admin-form-grid";
    const field = (label, key, span2) => {
        const l = document.createElement("label");
        if (span2) l.className = "span-2";
        l.append(document.createTextNode(label));
        l.append(buildAuthProviderInput(p.id, key, p[key] || "", label));
        return l;
    };
    grid.append(field("Label", "label"));
    if (p.provider === "microsoft") grid.append(field("Tenant ID", "tenant_id"));
    grid.append(field("Client ID", "client_id", true));
    grid.append(field("Client secret", "client_secret", true));

    card.append(head, grid, checkStatus);
    return card;
}

function buildAuthProviderInput(providerId, field, value, placeholder) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.placeholder = placeholder;
    input.style.width = "100%";
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

// Group account rows by email so a multi-account person reads as ONE row:
// email + person-level flags from the first account, plus one company chip
// per account. Actions (edit/view-as/delete) target the PERSONAL account
// when present, else the first account.
function _groupByEmail(users) {
    const by = new Map();
    for (const u of users) {
        if (!by.has(u.email)) by.set(u.email, []);
        by.get(u.email).push(u);
    }
    return [...by.values()].map((accounts) => {
        const primary = accounts.find((a) => !a.company_id) || accounts[0];
        const groups = [...new Set(accounts.flatMap((a) => a.groups || []))];
        return { primary, accounts, groups };
    });
}

function renderUsersTable(users) {
    const tbody = $("#admin-users-table tbody");
    tbody.innerHTML = "";
    const q = _userFilter.trim().toLowerCase();
    const people = _groupByEmail(users).filter(({ primary, accounts, groups }) => {
        if (!q) return true;
        return primary.email.toLowerCase().includes(q) ||
            accounts.some((a) => (a.display_name || "").toLowerCase().includes(q) ||
                (a.company_name || "").toLowerCase().includes(q)) ||
            groups.some((g) => g.toLowerCase().includes(q));
    });
    for (const { primary, accounts, groups } of people) {
        const u = primary;
        const tr = document.createElement("tr");

        const tdEmail = document.createElement("td");
        tdEmail.textContent = u.email;
        // Person-level badges + one chip per company account.
        tdEmail.appendChild(sourceBadges({
            is_super_admin: u.is_super_admin,
            native_allowed: u.native_allowed,
            company_id: "",
        }));
        for (const a of accounts) {
            if (a.company_id) {
                tdEmail.appendChild(sourceBadges({ company_id: a.company_id, company_name: a.company_name }));
            }
        }
        tr.appendChild(tdEmail);

        const tdName = document.createElement("td");
        tdName.textContent = u.display_name ||
            accounts.map((a) => a.display_name).find(Boolean) || "—";
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
        tdGroups.textContent = groups.length ? groups.join(", ") : "—";
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
                const n = accounts.length;
                if (!confirm(`Delete user ${u.email}?\n\nThis removes ${n > 1 ? `all ${n} of their accounts` : "their account"}, API keys, and folder grants. Folders they own become unowned.`)) return;
                try {
                    for (const a of accounts) await api.adminDeleteUser(a.id);
                }
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
        const total = _groupByEmail(users).length, shown = people.length;
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

let _editingGroupId = null;   // null = adding (when editor open)
let _groupEditorOpen = false;
let _gdMemberSelect = null;
let _groupFilter = "";

// List view: clickable group rows. The editor is a full pane that swaps with
// this list (same pattern as the user editor), not a side panel.
function renderGroups(groups, users) {
    const list = $("#admin-groups-list");
    if (!list) return;
    list.innerHTML = "";
    const q = _groupFilter.trim().toLowerCase();
    const rows = q ? groups.filter((g) => g.name.toLowerCase().includes(q)) : groups;
    for (const g of rows) {
        const li = document.createElement("li");
        li.className = "admin-group-row";
        const name = document.createElement("span");
        name.className = "admin-group-name";
        name.textContent = g.name;
        const count = document.createElement("span");
        count.className = "admin-group-count";
        count.textContent = `${g.member_count} member${g.member_count === 1 ? "" : "s"}`;
        li.append(name, count);
        li.addEventListener("click", () => openGroupEditor(g));
        list.appendChild(li);
    }
    const countEl = $("#admin-groups-count");
    if (countEl) {
        countEl.textContent = rows.length === groups.length
            ? `${groups.length} group${groups.length === 1 ? "" : "s"}`
            : `${rows.length} of ${groups.length} groups`;
    }

    // If the editor is open for an existing group, keep its member list fresh
    // after live add/remove (the WS push lands here). If that group was
    // deleted out from under us, bail back to the list.
    if (_groupEditorOpen && _editingGroupId != null) {
        const g = groups.find((x) => x.id === _editingGroupId);
        if (g) refreshGroupMembers(g, users);
        else closeGroupEditor();
    }
}

function openGroupEditor(group) {
    _groupEditorOpen = true;
    _editingGroupId = group ? group.id : null;
    $("#admin-group-editor-title").textContent = group ? "Edit group" : "New group";
    const nameEl = $("#admin-ge-name");
    const descEl = $("#admin-ge-desc");
    nameEl.value = group ? group.name : "";
    descEl.value = group ? (group.description || "") : "";
    $("#admin-ge-error").hidden = true;
    // Members only make sense once the group exists.
    $("#admin-ge-members-block").hidden = !group;
    $("#admin-ge-delete").hidden = !group;
    if (group) refreshGroupMembers(group, adminState.get()?.users || []);

    $("#admin-groups-list-view").hidden = true;
    $("#admin-group-editor").hidden = false;
    nameEl.focus();
}

function closeGroupEditor() {
    $("#admin-group-editor").hidden = true;
    $("#admin-groups-list-view").hidden = false;
    _groupEditorOpen = false;
    _editingGroupId = null;
    _gdMemberSelect = null;
}

// Members list + add-member picker for the (existing) group being edited.
function refreshGroupMembers(group, users) {
    const members = users.filter((u) => (u.groups || []).includes(group.name));
    $("#admin-ge-members-label").textContent = `Members (${members.length})`;
    const ul = $("#admin-ge-members");
    ul.innerHTML = "";
    for (const u of members) {
        const li = document.createElement("li");
        li.className = "admin-list-row";
        const label = document.createElement("span");
        label.textContent = u.display_name ? `${u.display_name} <${u.email}>` : u.email;
        const x = document.createElement("button");
        x.className = "admin-icon-btn admin-icon-danger";
        x.textContent = "✕";
        x.title = "Remove from group";
        x.addEventListener("click", async () => {
            try { await api.adminRemoveGroupMember(group.id, u.id); }
            catch (err) { alert(err.message); }
        });
        li.append(label, x);
        ul.appendChild(li);
    }

    const host = $("#admin-ge-addmember");
    host.innerHTML = "";
    const nonMembers = users.filter((u) => !(u.groups || []).includes(group.name));
    const byLabel = new Map(nonMembers.map((u) => [u.email, u.id]));
    _gdMemberSelect = createChipSelect({
        selected: [],
        options: () => [...byLabel.keys()],
        allowCreate: false,
        placeholder: "add a user by email…",
        onChange: async (vals) => {
            const email = vals[vals.length - 1];
            const uid = byLabel.get(email);
            if (uid == null) return;
            // Drop focus so the incoming admin.snapshot push isn't skipped by
            // the editing focus-guard — that's what refreshes the member list.
            if (document.activeElement) document.activeElement.blur();
            _gdMemberSelect.setValues([]);
            try { await api.adminAddGroupMember(group.id, uid); }
            catch (err) { alert(err.message); }
        },
    });
    host.appendChild(_gdMemberSelect.el);
}

async function saveGroupEditor() {
    const errEl = $("#admin-ge-error");
    errEl.hidden = true;
    const name = $("#admin-ge-name").value.trim();
    const description = $("#admin-ge-desc").value.trim();
    if (!name) { errEl.textContent = "Group name is required."; errEl.hidden = false; return; }
    try {
        if (_editingGroupId == null) {
            await api.adminCreateGroup(name, description);
        } else {
            await api.adminUpdateGroup(_editingGroupId, { name, description });
        }
        closeGroupEditor();
    } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
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

// Directory toggles: commit immediately on change. The Clerk toggle also
// carries whatever key is in the field; Save commits the key while keeping
// the current mode.
$("#admin-native-enabled").addEventListener("change", (e) =>
    saveNativeSettings(e.target.checked));
$("#admin-clerk-enabled").addEventListener("change", (e) =>
    saveClerkSettings(e.target.checked));
$("#admin-clerk-save").addEventListener("click", () =>
    saveClerkSettings($("#admin-clerk-enabled").checked));
$("#admin-clerk-key").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
        e.preventDefault();
        saveClerkSettings($("#admin-clerk-enabled").checked);
    }
});
$("#admin-clerk-user-filter").addEventListener("input", (e) => {
    _clerkUserFilter = e.target.value;
    renderClerkUsersTable();
});

// Users tab: filter + add + slide-over editor.
$("#admin-user-filter").addEventListener("input", (e) => {
    _userFilter = e.target.value;
    renderUsersTable(adminState.get()?.users || []);
});
$("#admin-user-add-btn").addEventListener("click", () => openUserEditor(null));
$("#admin-ue-cancel").addEventListener("click", closeUserEditor);
$("#admin-ue-back").addEventListener("click", closeUserEditor);
$("#admin-ue-save").addEventListener("click", saveUserEditor);

// Groups tab: filter + new + full-pane editor (mirrors Users).
$("#admin-group-filter").addEventListener("input", (e) => {
    _groupFilter = e.target.value;
    renderGroups(adminState.get()?.groups || [], adminState.get()?.users || []);
});
$("#admin-group-add-btn").addEventListener("click", () => openGroupEditor(null));
$("#admin-ge-back").addEventListener("click", closeGroupEditor);
$("#admin-ge-cancel").addEventListener("click", closeGroupEditor);
$("#admin-ge-save").addEventListener("click", saveGroupEditor);
$("#admin-ge-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveGroupEditor(); }
});
$("#admin-ge-delete").addEventListener("click", async () => {
    if (_editingGroupId == null) return;
    const g = (adminState.get()?.groups || []).find((x) => x.id === _editingGroupId);
    if (!g) return;
    if (!confirm(`Delete group "${g.name}"?\n\nMembers are not deleted — they just lose this group.`)) return;
    try {
        await api.adminDeleteGroup(_editingGroupId);
        closeGroupEditor();
    } catch (err) { alert(err.message); }
});

$("#btn-stop-impersonate").addEventListener("click", async () => {
    try {
        await api.adminStopImpersonate();
        window.location.reload();
    } catch (err) { alert(err.message); }
});
