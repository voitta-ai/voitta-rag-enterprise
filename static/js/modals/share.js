// ----- Share modal — audience / groups / people -----
//
// Visibility is the UNION of independent layers (server:
// services/acl/folder_acl.py): the audience share (Clerk org or
// native-everyone), group shares (voitta-native groups), and people
// shares (emails — NOT restricted to the org; unknown addresses are
// live "pending" shares that materialise on first sign-in). Layers
// never mask each other, so nothing here dims anything else.
//
// Sections shown per the owner's community (server decides via
// audience.kind): "clerk_org" → Organization + People;
// "native_all" → Everyone + Groups + People; "none" → People only.
//
// Every action applies immediately (one API call each, server pushes
// folder.upserted so tree pills update everywhere); Done just closes.

import { api } from "../api.js";
import { buildSwitch } from "../dom/switch.js";
import { folders } from "../store.js";

const $ = (sel) => document.querySelector(sel);

let _folderId = null;
let _groupsCache = null; // org group pick-list, fetched once per open

export async function openShareModal(folder) {
    _folderId = folder.id;
    _groupsCache = null;
    $("#share-title").textContent = `Share — ${folder.display_name}`;
    $("#share-error").hidden = true;
    $("#share-email-input").value = "";
    // Hide all conditional sections until the config lands (prevents a
    // flash of the wrong variant on slow loads / reopens).
    $("#share-audience-section").hidden = true;
    $("#share-no-audience").hidden = true;
    $("#share-groups-section").hidden = true;
    $("#share-group-list").replaceChildren();
    $("#share-people-list").replaceChildren();
    $("#share-summary").textContent = "";
    $("#share-backdrop").hidden = false;
    try {
        render(await api.getSharing(folder.id));
    } catch (err) {
        showError(err.message);
    }
}

function closeShareModal() {
    $("#share-backdrop").hidden = true;
    _folderId = null;
}

function showError(msg) {
    const el = $("#share-error");
    el.textContent = msg;
    el.hidden = false;
}

// Wrap a mutation: apply, re-render from the server's response, surface
// failures inline without closing the modal.
async function apply(action) {
    $("#share-error").hidden = true;
    try {
        render(await action());
    } catch (err) {
        showError(err.message);
    }
}

function render(sharing) {
    if (sharing.folder_id !== _folderId) return; // stale response after switch
    renderAudience(sharing.audience);
    renderGroups(sharing);
    renderPeople(sharing.people);
    renderSummary(sharing);
}

function renderAudience(audience) {
    const section = $("#share-audience-section");
    const none = $("#share-no-audience");
    if (audience.kind === "none") {
        section.hidden = true;
        none.hidden = false;
        return;
    }
    none.hidden = true;
    section.hidden = false;
    $("#share-audience-heading").textContent =
        audience.kind === "native_all" ? "Everyone" : "Organization";
    $("#share-audience-label").textContent = audience.label;
    $("#share-audience-hint").textContent =
        "Can view this folder and search its contents";
    const slot = $("#share-audience-switch-slot");
    const sw = buildSwitch({
        title: audience.on ? "Shared — click to unshare" : "Click to share",
        checked: audience.on,
        disabled: false,
        onChange: (next) =>
            apply(async () => {
                await api.setFolderShare(_folderId, next);
                return api.getSharing(_folderId);
            }),
    });
    slot.replaceChildren(sw);
}

async function ensureGroupsCache() {
    if (_groupsCache === null) _groupsCache = await api.listShareGroups();
    return _groupsCache;
}

function renderGroups(sharing) {
    const section = $("#share-groups-section");
    // Groups are a voitta-native concept — hidden for Clerk-org owners.
    if (sharing.audience.kind === "clerk_org") {
        section.hidden = true;
        return;
    }
    section.hidden = false;

    const list = $("#share-group-list");
    list.replaceChildren();
    for (const g of sharing.groups) {
        list.append(
            buildRow({
                glyph: "◆",
                label: g.name,
                detail: `${g.member_count} member${g.member_count === 1 ? "" : "s"}`,
                onRemove: () => apply(() => api.unshareGroup(_folderId, g.id)),
            }),
        );
    }

    // Pick-list: org groups not already granted.
    const sel = $("#share-group-select");
    const grantedIds = new Set(sharing.groups.map((g) => g.id));
    ensureGroupsCache()
        .then((all) => {
            sel.replaceChildren();
            const opts = all.filter((g) => !grantedIds.has(g.id));
            const head = document.createElement("option");
            head.value = "";
            head.disabled = true;
            head.selected = true;
            head.textContent = opts.length ? "Select a group…" : "No more groups";
            sel.append(head);
            for (const g of opts) {
                const o = document.createElement("option");
                o.value = String(g.id);
                o.textContent = `${g.name} (${g.member_count})`;
                sel.append(o);
            }
        })
        .catch((err) => showError(err.message));
}

function renderPeople(people) {
    const list = $("#share-people-list");
    list.replaceChildren();
    for (const p of people) {
        const badges = [];
        badges.push(p.status === "member" ? "member" : "pending");
        if (p.outside_org) badges.push("outside org");
        list.append(
            buildRow({
                glyph: p.status === "member" ? "●" : "○",
                label: p.email,
                detail: badges.join(" · "),
                pending: p.status !== "member",
                onRemove: () => apply(() => api.unshareEmail(_folderId, p.email)),
            }),
        );
    }
}

function buildRow({ glyph, label, detail, pending, onRemove }) {
    const li = document.createElement("li");
    li.className = "share-row" + (pending ? " share-row-pending" : "");
    const g = document.createElement("span");
    g.className = "share-row-glyph";
    g.textContent = glyph;
    const name = document.createElement("span");
    name.className = "share-row-label";
    name.textContent = label;
    const d = document.createElement("span");
    d.className = "share-row-detail hint";
    d.textContent = detail;
    const rm = document.createElement("button");
    rm.className = "btn btn-secondary btn-sm";
    rm.textContent = "Remove";
    rm.addEventListener("click", onRemove);
    li.append(g, name, d, rm);
    return li;
}

function renderSummary(sharing) {
    const parts = [];
    if (sharing.audience.on) {
        parts.push(sharing.audience.kind === "native_all" ? "Everyone" : "Org");
    }
    if (sharing.groups.length) {
        parts.push(`${sharing.groups.length} group${sharing.groups.length === 1 ? "" : "s"}`);
    }
    if (sharing.people.length) {
        const outside = sharing.people.filter((p) => p.outside_org).length;
        parts.push(
            `${sharing.people.length} ${sharing.people.length === 1 ? "person" : "people"}` +
                (outside ? ` (${outside} outside org)` : ""),
        );
    }
    $("#share-summary").textContent = parts.length
        ? `Access: ${parts.join(" + ")}`
        : "Private — only you";
}

// ---------------------------------------------------------------------------
// Static wiring (script-load once)
// ---------------------------------------------------------------------------

$("#share-close").addEventListener("click", closeShareModal);
$("#share-done").addEventListener("click", closeShareModal);
$("#share-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "share-backdrop") closeShareModal();
});

$("#share-group-add").addEventListener("click", () => {
    const v = $("#share-group-select").value;
    if (!v) return;
    apply(() => api.shareToGroup(_folderId, Number(v)));
});

function addEmail() {
    const email = $("#share-email-input").value.trim();
    if (!email) return;
    if (!email.includes("@") || email.includes(" ")) {
        showError("Enter a valid email address.");
        return;
    }
    $("#share-email-input").value = "";
    apply(() => api.shareToEmail(_folderId, email));
}

$("#share-email-add").addEventListener("click", addEmail);
$("#share-email-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addEmail();
});

// Keep an open modal's title in step with renames arriving over WS.
folders.subscribe((list) => {
    if (_folderId === null) return;
    const f = list.find((x) => x.id === _folderId);
    if (f) $("#share-title").textContent = `Share — ${f.display_name}`;
});
