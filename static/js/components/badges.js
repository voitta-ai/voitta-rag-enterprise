// Sign-in provenance badges — shared by the top-bar user pill and the
// admin Users table. Account-model rules:
//
// - SUPERADMIN (env-listed email) always shows, regardless of account.
// - Personal account (company_id === ""): VOITTA NATIVE when the email is
//   on the local allowlist, otherwise CLERK (an org-less Clerk user whose
//   only account is Personal).
// - Company account: one chip with the company name.
//
// ``info`` fields: is_super_admin, native_allowed, company_id, company_name.

export function sourceBadges(info) {
    const frag = document.createDocumentFragment();
    if (!info) return frag;
    const add = (text, variant, title) => {
        const b = document.createElement("span");
        b.className = `src-badge src-badge-${variant}`;
        b.textContent = text;
        if (title) b.title = title;
        frag.appendChild(b);
    };
    if (info.is_super_admin) {
        add("SUPERADMIN", "super", "From VOITTA_SUPER_ADMINS — always admitted, admin re-stamped on every login.");
    }
    if (info.company_id) {
        add(info.company_name || "COMPANY", "clerk",
            "Company account (from the Clerk directory). Folders, grants and API keys are scoped to it.");
    } else if (info.native_allowed) {
        add("VOITTA NATIVE", "native", "Personal account — admitted by the native allowlist (user or domain).");
    } else if (!info.is_super_admin) {
        add("CLERK", "clerk", "Personal account of a Clerk-directory user (no native allowlist entry).");
    }
    return frag;
}
