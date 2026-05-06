// REST helpers. Identity comes from the signed session cookie set by the
// Google login flow (Sign in with Google). When deployed behind a
// reverse-proxy auth tier the proxy injects X-Forwarded-Email on the
// server side — the browser must NEVER do that itself, otherwise logout
// is a no-op (the header keeps re-asserting an identity).

async function req(method, path, body) {
    const opts = {
        method,
        // Same-origin cookies are the default for fetch, but state it
        // explicitly so behaviour is obvious to anyone reading the code.
        credentials: "same-origin",
        headers: {
            "Accept": "application/json",
        },
    };
    if (body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    if (!r.ok) {
        const text = await r.text();
        throw new Error(`${r.status} ${path}: ${text}`);
    }
    if (r.status === 204) return null;
    const ct = r.headers.get("content-type") || "";
    return ct.includes("application/json") ? r.json() : r.text();
}

export const api = {
    root: () => req("GET", "/api/folders/root"),
    listFolders: () => req("GET", "/api/folders"),
    addFolderByName: (name) => req("POST", "/api/folders", { name }),
    deleteFolder: (id) => req("DELETE", `/api/folders/${id}`),
    setFolderShare: (id, shared) => req("PATCH", `/api/folders/${id}/share`, { shared }),
    setFolderActive: (id, active) => req("PATCH", `/api/folders/${id}/active`, { active }),
    listFiles: (folderId) => req("GET", `/api/folders/${folderId}/files`),
    folderStats: (folderId) => req("GET", `/api/folders/${folderId}/stats`),
    listAllFiles: async () => {
        const fs = await req("GET", "/api/folders");
        const all = await Promise.all(fs.map(f => req("GET", `/api/folders/${f.id}/files`)));
        return all.flat();
    },
    upload: async (folderId, files, relDir = "", onProgress = null) => {
        // XMLHttpRequest because fetch doesn't expose upload progress
        // events. ``onProgress({loaded, total, fraction})`` is called
        // repeatedly while the FormData body is being sent; the server
        // reads the upload before the response is built, so loaded == total
        // means the bytes are off the client and the user is now waiting on
        // server-side disk writes.
        const batch = Array.from(files);
        const form = new FormData();
        for (const file of batch) form.append("file", file);
        const query = relDir ? `?rel_dir=${encodeURIComponent(relDir)}` : "";
        const url = `/api/folders/${folderId}/upload${query}`;
        return await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", url);
            xhr.withCredentials = true;  // send the session cookie
            xhr.responseType = "json";
            if (onProgress) {
                xhr.upload.addEventListener("progress", (e) => {
                    if (!e.lengthComputable) return;
                    onProgress({
                        loaded: e.loaded,
                        total: e.total,
                        fraction: e.total ? e.loaded / e.total : 0,
                    });
                });
            }
            xhr.addEventListener("load", () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(xhr.response);
                } else {
                    const detail = xhr.response?.detail || xhr.responseText || "";
                    reject(new Error(`${xhr.status} upload: ${detail}`));
                }
            });
            xhr.addEventListener("error", () => reject(new Error("upload network error")));
            xhr.addEventListener("abort", () => reject(new Error("upload aborted")));
            xhr.send(form);
        });
    },
    mkdir: (folderId, path) =>
        req("POST", `/api/folders/${folderId}/mkdir`, { path }),
    reindexFolder: (folderId, relDir = "") =>
        req("POST", `/api/folders/${folderId}/reindex`, { rel_dir: relDir || null }),
    getSync: (folderId) => req("GET", `/api/folders/${folderId}/sync`),
    putSync: (folderId, body) => req("PUT", `/api/folders/${folderId}/sync`, body),
    deleteSync: (folderId) => req("DELETE", `/api/folders/${folderId}/sync`),
    triggerSync: (folderId) => req("POST", `/api/folders/${folderId}/sync/trigger`),
    listGitBranches: (folderId, body) =>
        req("POST", `/api/folders/${folderId}/sync/branches`, body),
    gdAuthInit: (folderId) =>
        req("POST", `/api/folders/${folderId}/sync/google-drive/auth`),
    gdListFolders: (folderId) =>
        req("GET", `/api/folders/${folderId}/sync/google-drive/folders`),
    authConfig: () => req("GET", "/api/auth/config"),
    me: () => req("GET", "/api/auth/me"),
    logout: () => req("POST", "/api/auth/logout"),
    listKeys: () => req("GET", "/api/auth/keys"),
    createKey: (name) => req("POST", "/api/auth/keys", { name }),
    deleteKey: (id) => req("DELETE", `/api/auth/keys/${id}`),
    recentJobs: () => req("GET", "/api/jobs/recent?limit=50"),
    retryJob: (id) => req("POST", `/api/jobs/${id}/retry`),
    retryAllFailed: () => req("POST", "/api/jobs/retry-failed"),
    cleanupFailedJobs: () => req("DELETE", "/api/jobs/cleanup-failed"),
    search: (query, modes = ["chunks"], folderIds = null, limit = 10) =>
        req("POST", "/api/search", { query, modes, folder_ids: folderIds, limit }),

    // Admin
    adminAllowlist: () => req("GET", "/api/admin/allowlist"),
    adminAddDomain: (domain) => req("POST", "/api/admin/allowlist/domains", { domain }),
    adminRemoveDomain: (domain) =>
        req("DELETE", `/api/admin/allowlist/domains/${encodeURIComponent(domain)}`),
    adminAddUser: (email) => req("POST", "/api/admin/allowlist/users", { email }),
    adminRemoveUser: (email) =>
        req("DELETE", `/api/admin/allowlist/users/${encodeURIComponent(email)}`),
    adminBlock: (email) => req("POST", "/api/admin/blocklist", { email }),
    adminUnblock: (email) =>
        req("DELETE", `/api/admin/blocklist/${encodeURIComponent(email)}`),
    adminListUsers: () => req("GET", "/api/admin/users"),
    adminSetIsAdmin: (id, is_admin) =>
        req("PATCH", `/api/admin/users/${id}`, { is_admin }),
    adminImpersonate: (id) => req("POST", `/api/admin/impersonate/${id}`),
    adminStopImpersonate: () => req("DELETE", "/api/admin/impersonate"),
};
