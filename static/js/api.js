// REST helpers. Auth: every request carries X-Forwarded-Email so the
// dev/multi-user backend resolves a user. The browser stores the email in
// localStorage; default is "browser@localhost".

const EMAIL_KEY = "voitta-image-rag.email";
export function userEmail() {
    return localStorage.getItem(EMAIL_KEY) || "browser@localhost";
}

async function req(method, path, body) {
    const opts = {
        method,
        headers: {
            "X-Forwarded-Email": userEmail(),
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
    fsList: (path) => req("GET", `/api/fs/list${path ? `?path=${encodeURIComponent(path)}` : ""}`),
    listFolders: () => req("GET", "/api/folders"),
    addFolderByPath: (path) => req("POST", "/api/folders", { path }),
    addFolderByName: (name) => req("POST", "/api/folders", { name }),
    deleteFolder: (id) => req("DELETE", `/api/folders/${id}`),
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
            xhr.setRequestHeader("X-Forwarded-Email", userEmail());
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
    recentJobs: () => req("GET", "/api/jobs/recent?limit=50"),
    retryJob: (id) => req("POST", `/api/jobs/${id}/retry`),
    retryAllFailed: () => req("POST", "/api/jobs/retry-failed"),
    cleanupFailedJobs: () => req("DELETE", "/api/jobs/cleanup-failed"),
    search: (query, modes = ["chunks"], folderIds = null, limit = 10) =>
        req("POST", "/api/search", { query, modes, folder_ids: folderIds, limit }),
};
