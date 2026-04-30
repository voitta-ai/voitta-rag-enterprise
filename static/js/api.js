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
    upload: async (folderId, file, relDir = "") => {
        const form = new FormData();
        form.append("file", file);
        const relPath = relDir ? `${relDir}/${file.name}` : file.name;
        const url = `/api/folders/${folderId}/upload?rel_path=${encodeURIComponent(relPath)}`;
        const r = await fetch(url, {
            method: "POST",
            headers: { "X-Forwarded-Email": userEmail() },
            body: form,
        });
        if (!r.ok) throw new Error(`${r.status} upload: ${await r.text()}`);
        return r.json();
    },
    mkdir: (folderId, path) =>
        req("POST", `/api/folders/${folderId}/mkdir`, { path }),
    reindexFolder: (folderId, relDir = "") =>
        req("POST", `/api/folders/${folderId}/reindex`, { rel_dir: relDir || null }),
    recentJobs: () => req("GET", "/api/jobs/recent?limit=50"),
    retryJob: (id) => req("POST", `/api/jobs/${id}/retry`),
    retryAllFailed: () => req("POST", "/api/jobs/retry-failed"),
    cleanupFailedJobs: () => req("DELETE", "/api/jobs/cleanup-failed"),
    search: (query, modes = ["chunks"], folderIds = null, limit = 10) =>
        req("POST", "/api/search", { query, modes, folder_ids: folderIds, limit }),
};
