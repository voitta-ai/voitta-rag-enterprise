// Authentication gate.
//
// ``ensureAuthenticated`` is the bootstrap's first call: it asks the
// server who the user is, paints the user pill / Admin button /
// impersonation banner, and returns ``true`` to let bootstrap proceed.
// On 401, it renders the login gate (Sign-in button or operator hint
// when Google client isn't configured) and returns ``false`` so the
// rest of bootstrap bails.

import { api } from "../api.js";
import { me as meStore } from "../store.js";
import { sourceBadges } from "../components/badges.js";

const $ = (sel) => document.querySelector(sel);

export async function ensureAuthenticated() {
    // Returns true when the user is signed in (or auth is bypassed via
    // VOITTA_SINGLE_USER / VOITTA_DEV_USER / forwarded headers); false when
    // we rendered the login gate and the rest of bootstrap should bail.
    try {
        const me = await api.me();
        meStore.set(me);
        $("#user-pill").textContent = me.email;
        $("#user-pill").hidden = false;
        // Provenance badges for the ACTIVE account: SUPERADMIN /
        // VOITTA NATIVE / company chip.
        const badges = $("#user-badges");
        badges.innerHTML = "";
        badges.appendChild(sourceBadges(me));
        renderAccountSelect(me);
        $("#btn-logout").hidden = false;
        // Admin button is gated on the *real* user's flag — impersonation
        // never grants admin powers. The /api/auth/me endpoint enforces
        // the same: ``is_admin`` reflects the real identity.
        $("#btn-admin").hidden = !me.is_admin;
        // Impersonation banner: visible iff an admin has chosen "view as".
        if (me.acting_as_user_id) {
            $("#impersonate-text").textContent =
                `Viewing the app as ${me.acting_as_email} — your own admin status is unaffected.`;
            $("#impersonate-banner").hidden = false;
        } else {
            $("#impersonate-banner").hidden = true;
        }
        $("#login-gate").hidden = true;
        return true;
    } catch (err) {
        if (!String(err.message || "").startsWith("401")) {
            console.error("auth/me failed", err);
            return true; // not an auth issue — let the app try to load
        }
    }
    // 401: render the gate. Hide the Sign-in button if the server has no
    // Google client configured; the help text tells the operator how to fix.
    let cfg;
    try { cfg = await api.authConfig(); } catch { cfg = { google_enabled: false }; }
    if (!cfg.google_enabled) {
        $("#login-gate-google").hidden = true;
        $("#login-gate-disabled").hidden = false;
    }
    $("#login-gate").hidden = false;
    return false;
}

// Company dropdown: one entry per account (Personal + each Clerk company).
// Hidden for single-account users. Switching POSTs the account id and hard
// reloads — every store (folders, files, WS) re-keys to the new identity.
function renderAccountSelect(me) {
    const sel = $("#account-select");
    if (!sel) return;
    const accounts = me.accounts || [];
    if (accounts.length < 2) { sel.hidden = true; return; }
    sel.innerHTML = "";
    for (const a of accounts) {
        const opt = document.createElement("option");
        opt.value = String(a.id);
        opt.textContent = a.company_id ? (a.company_name || a.company_id) : "Personal";
        if (a.id === me.id) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.hidden = false;
    if (!sel._bound) {
        sel._bound = true;
        sel.addEventListener("change", async () => {
            try {
                await api.switchAccount(Number(sel.value));
                window.location.reload();
            } catch (err) {
                alert(err.message || "account switch failed");
            }
        });
    }
}

$("#btn-logout").addEventListener("click", async () => {
    try { await api.logout(); } catch (err) { console.warn("logout failed", err); }
    // Hard reload so any in-memory state (folders, files, ws connection) is
    // dropped and the gate re-renders cleanly.
    window.location.reload();
});
