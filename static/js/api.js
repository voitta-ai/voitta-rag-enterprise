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
    health: () => req("GET", "/api/health"),
    listFolders: () => req("GET", "/api/folders"),
    // One-shot bootstrap of the activeFolders set. Subsequent updates
    // arrive via the folder.active_changed WS event.
    activeFolderIds: () => req("GET", "/api/folders/active-ids"),
    addFolderByName: (name) => req("POST", "/api/folders", { name }),
    deleteFolder: (id) => req("DELETE", `/api/folders/${id}`),
    listFolderDirs: (folderId) => req("GET", `/api/folders/${folderId}/dirs`),
    deleteFile: (folderId, fileId) => req("DELETE", `/api/folders/${folderId}/files/${fileId}`),
    deleteSubdir: (folderId, rel) => req("DELETE", `/api/folders/${folderId}/dirs?rel=${encodeURIComponent(rel)}`),
    setFolderShare: (id, shared) => req("PATCH", `/api/folders/${id}/share`, { shared }),
    setFolderActive: (id, active) => req("PATCH", `/api/folders/${id}/active`, { active }),
    // ``name`` is optional — omit it (or pass undefined, which JSON.stringify
    // drops) for a label-only rename that never touches disk; include it to
    // also rename the directory under VOITTA_ROOT_PATH.
    renameFolder: (id, { display_name, name }) =>
        req("PATCH", `/api/folders/${id}/rename`, { display_name, name }),
    listFiles: (folderId) => req("GET", `/api/folders/${folderId}/files`),
    folderStats: (folderId, dir) => req(
        "GET",
        `/api/folders/${folderId}/stats${dir ? `?dir=${encodeURIComponent(dir)}` : ""}`,
    ),
    listAllFiles: async () => {
        const fs = await req("GET", "/api/folders");
        const all = await Promise.all(fs.map(f => req("GET", `/api/folders/${f.id}/files`)));
        return all.flat();
    },
    uploadOne: (folderId, file, relDir = "", onProgress = null) => {
        // One file per POST. The server (folders.upload_file) streams the
        // body straight to disk and atomically renames the sidecar into
        // place — so as soon as this Promise resolves, the file exists at
        // its final path and the watcher will fire ``file.upserted``.
        // Smaller-than-batch POSTs also give us per-file XHR progress
        // events instead of a single aggregate fraction.
        const form = new FormData();
        // Explicit basename override: Chrome's <input webkitdirectory>
        // populates the multipart ``filename`` header with the file's
        // full webkitRelativePath ("research/images/foo.jpg"), and
        // ``_safe_filename`` on the backend rejects anything with a
        // slash. The directory component is already on the wire as
        // the ``rel_dir`` query param, so pass only the basename here.
        form.append("file", file, file.name);
        const query = relDir ? `?rel_dir=${encodeURIComponent(relDir)}` : "";
        const url = `/api/folders/${folderId}/upload${query}`;
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", url);
            xhr.withCredentials = true;
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
    uploadBatch: async (
        folderId, files, relDir = "",
        { concurrency = 3, onFileProgress = null, onFileDone = null, onFileError = null } = {},
    ) => {
        // Drive N parallel uploadOne calls with a small concurrency cap.
        // Per-file callbacks let the UI render a row per file ("a.pdf —
        // 42%" / "✓ a.pdf"); the watcher's ``file.upserted`` events also
        // populate the folder's file list as each file lands.
        //
        // ``files`` can be either:
        //   * ``File[]`` — all uploaded into ``relDir``.
        //   * ``{file: File, relDir: string}[]`` — per-entry target
        //     subdirectory. Used by the directory picker + drag-and-drop
        //     paths so nested folder structure is preserved on the server.
        const list = Array.from(files).map((entry) =>
            entry instanceof File
                ? { file: entry, relDir }
                : { file: entry.file, relDir: entry.relDir ?? relDir }
        );
        let next = 0;
        const failures = [];
        async function worker() {
            while (true) {
                const idx = next++;
                if (idx >= list.length) return;
                const { file, relDir: perFileRelDir } = list[idx];
                try {
                    const resp = await api.uploadOne(
                        folderId, file, perFileRelDir,
                        onFileProgress
                            ? (p) => onFileProgress(idx, file, p)
                            : null,
                    );
                    if (onFileDone) onFileDone(idx, file, resp);
                } catch (err) {
                    failures.push({ idx, file, err });
                    if (onFileError) onFileError(idx, file, err);
                }
            }
        }
        const cap = Math.max(1, Math.min(concurrency, list.length));
        await Promise.all(Array.from({ length: cap }, () => worker()));
        return { count: list.length, failures };
    },
    mkdir: (folderId, path) =>
        req("POST", `/api/folders/${folderId}/mkdir`, { path }),
    // states: null = full reindex; array (e.g. ["error","unsupported"]) =
    // only files currently in those states ("reindex light").
    reindexFolder: (folderId, relDir = "", states = null) =>
        req("POST", `/api/folders/${folderId}/reindex`,
            { rel_dir: relDir || null, states }),
    getSync: (folderId) => req("GET", `/api/folders/${folderId}/sync`),
    putSync: (folderId, body) => req("PUT", `/api/folders/${folderId}/sync`, body),
    deleteSync: (folderId) => req("DELETE", `/api/folders/${folderId}/sync`),
    clearSyncError: (folderId) => req("DELETE", `/api/folders/${folderId}/sync/error`),
    triggerSync: (folderId) => req("POST", `/api/folders/${folderId}/sync/trigger`),
    listGitBranches: (folderId, body) =>
        req("POST", `/api/folders/${folderId}/sync/branches`, body),
    gdAuthInit: (folderId) =>
        req("POST", `/api/folders/${folderId}/sync/google-drive/auth`),
    gdListFolders: (folderId) =>
        req("GET", `/api/folders/${folderId}/sync/google-drive/folders`),
    gdBrowseFolder: (folderId, parentId, driveId = "") =>
        req("GET", `/api/folders/${folderId}/sync/google-drive/browse?parent_id=${encodeURIComponent(parentId)}${driveId ? `&drive_id=${encodeURIComponent(driveId)}` : ""}`),
    gdApiStatus: (folderId) =>
        req("GET", `/api/folders/${folderId}/sync/google-drive/api-status`),
    // NFS — capability probe + scoped directory picker. The picker
    // walks one level at a time so a deep tree doesn't fetch all at
    // once; the server resolves and validates every ``rel`` against
    // the admin's NFS root before returning entries.
    nfsStatus: () => req("GET", "/api/sync/nfs/status"),
    nfsBrowse: (rel = "") =>
        req("GET", `/api/sync/nfs/browse?rel=${encodeURIComponent(rel || "")}`),
    // Google Drive LOCAL (desktop, no credentials). Enumerate signed-in
    // accounts, browse the (free, stub) tree one level at a time, then
    // register the chosen subtree as an indexed-in-place folder. Read-only:
    // nothing is ever written back into the Drive mount.
    gdlAccounts: () => req("GET", "/api/sync/local/accounts"),
    gdlBrowse: (path) =>
        req("GET", `/api/sync/local/browse?path=${encodeURIComponent(path)}`),
    gdlConnect: (body) => req("POST", "/api/sync/local/connect", body),
    msAuthInit: (folderId) =>
        req("POST", `/api/folders/${folderId}/sync/microsoft/auth`),
    msListSites: (folderId) =>
        req("GET", `/api/folders/${folderId}/sync/microsoft/sites`),
    msListUsers: (folderId) =>
        req("GET", `/api/folders/${folderId}/sync/microsoft/users`),
    msScopeCheck: (folderId) =>
        req("GET", `/api/folders/${folderId}/sync/microsoft/scope-check`),
    jiraListProjects: (folderId, query = "") =>
        req("GET", `/api/folders/${folderId}/sync/jira/projects`
            + (query ? `?query=${encodeURIComponent(query)}` : "")),
    confluenceListSpaces: (folderId, query = "") =>
        req("GET", `/api/folders/${folderId}/sync/confluence/spaces`
            + (query ? `?query=${encodeURIComponent(query)}` : "")),
    authConfig: () => req("GET", "/api/auth/config"),
    me: () => req("GET", "/api/auth/me"),
    logout: () => req("POST", "/api/auth/logout"),
    listKeys: () => req("GET", "/api/auth/keys"),
    createKey: (name) => req("POST", "/api/auth/keys", { name }),
    deleteKey: (id) => req("DELETE", `/api/auth/keys/${id}`),
    // Company keys — 403 for non-admins; the settings modal hides the section.
    listCompanyKeys: () => req("GET", "/api/auth/company-keys"),
    createCompanyKey: (name) => req("POST", "/api/auth/company-keys", { name }),
    deleteCompanyKey: (id) => req("DELETE", `/api/auth/company-keys/${id}`),
    recentJobs: () => req("GET", "/api/jobs/recent?limit=50"),
    retryJob: (id) => req("POST", `/api/jobs/${id}/retry`),
    retryAllFailed: () => req("POST", "/api/jobs/retry-failed"),
    cleanupFailedJobs: () => req("DELETE", "/api/jobs/cleanup-failed"),
    cancelAllJobs: () => req("POST", "/api/jobs/cancel-all"),
    cancelJob: (id) => req("POST", `/api/jobs/${id}/cancel`),
    search: (query, modes = ["chunks"], folderIds = null, limit = 10) =>
        req("POST", "/api/search", { query, modes, folder_ids: folderIds, limit }),

    // File preview
    fileDownloadUrl: (fileId) => `/api/files/${fileId}/raw`,
    filePageImages: (fileId) => req("GET", `/api/files/${fileId}/page-images`),
    fileImages: (fileId) => req("GET", `/api/files/${fileId}/images`),
    fileLayout: (fileId) => req("GET", `/api/files/${fileId}/layout`),
    fileMeshUrl: (fileId) => `/api/files/${fileId}/stl`,
    fileEmail: (fileId) => req("GET", `/api/files/${fileId}/email`),

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
    adminCreateUser: (email, is_admin) =>
        req("POST", "/api/admin/users", { email, is_admin, grant_signin: true }),
    adminSetIsAdmin: (id, is_admin) =>
        req("PATCH", `/api/admin/users/${id}`, { is_admin }),
    // Partial update: any of { is_admin, display_name, groups }.
    adminUpdateUser: (id, body) => req("PATCH", `/api/admin/users/${id}`, body),
    adminDeleteUser: (id) => req("DELETE", `/api/admin/users/${id}`),
    adminImpersonate: (id) => req("POST", `/api/admin/impersonate/${id}`),
    adminStopImpersonate: () => req("DELETE", "/api/admin/impersonate"),
    // Groups (organizational)
    adminListGroups: () => req("GET", "/api/admin/groups"),
    adminCreateGroup: (name, description) =>
        req("POST", "/api/admin/groups", { name, description }),
    adminUpdateGroup: (id, body) => req("PATCH", `/api/admin/groups/${id}`, body),
    adminDeleteGroup: (id) => req("DELETE", `/api/admin/groups/${id}`),
    adminAddGroupMember: (id, user_id) =>
        req("POST", `/api/admin/groups/${id}/members`, { user_id }),
    adminRemoveGroupMember: (id, user_id) =>
        req("DELETE", `/api/admin/groups/${id}/members/${user_id}`),

    // Admin — auth providers (OAuth credentials catalog). Just a list;
    // not wired into the login flow (yet).
    adminListAuthProviders: () => req("GET", "/api/admin/auth-providers"),
    adminCreateAuthProvider: (body) => req("POST", "/api/admin/auth-providers", body),
    adminUpdateAuthProvider: (id, body) =>
        req("PATCH", `/api/admin/auth-providers/${id}`, body),
    adminDeleteAuthProvider: (id) => req("DELETE", `/api/admin/auth-providers/${id}`),
    adminCheckAuthProvider: (id) => req("POST", `/api/admin/auth-providers/${id}/check`),

    // Admin — indexing caps. GET returns {values, defaults, bounds};
    // PATCH accepts a partial dict of integer overrides.
    adminGetIndexingCaps: () => req("GET", "/api/admin/indexing-caps"),
    adminUpdateIndexingCaps: (partial) => req("PATCH", "/api/admin/indexing-caps", partial),

    // Admin — typed settings (currently: NFS root). The PATCH is
    // partial: send {nfs_root: ""} to disable, send {nfs_root: "/mnt/x"}
    // to enable + validate. The server checks existence + read access
    // at write time and refuses bad paths.
    adminGetSettings: () => req("GET", "/api/admin/settings"),
    adminUpdateSettings: (partial) => req("PATCH", "/api/admin/settings", partial),

    // Admin — Clerk directory (read-only proxy; requires the Clerk
    // toggle + secret key on the Sign-in gate tab).
    adminClerkDirectory: () => req("GET", "/api/admin/clerk/directory"),
    // Super-admin only: impersonate a Clerk-directory user (provisions
    // their accounts on the fly; company_id '' = Personal).
    adminClerkImpersonate: (email, companyId = "") =>
        req("POST", "/api/admin/clerk/impersonate", { email, company_id: companyId }),

    // Account switch — pick which (email, company) account is active.
    switchAccount: (accountId) => req("POST", `/api/auth/account/${accountId}`),
};
