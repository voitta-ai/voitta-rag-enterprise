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
    listFolders: () => req("GET", "/api/folders"),
    addFolderByPath: (path) => req("POST", "/api/folders", { path }),
    addFolderByName: (name) => req("POST", "/api/folders", { name }),
    deleteFolder: (id) => req("DELETE", `/api/folders/${id}`),
    listFiles: (folderId) => req("GET", `/api/folders/${folderId}/files`),
    listAllFiles: async () => {
        const fs = await req("GET", "/api/folders");
        const all = await Promise.all(fs.map(f => req("GET", `/api/folders/${f.id}/files`)));
        return all.flat();
    },
    upload: async (folderId, file) => {
        const form = new FormData();
        form.append("file", file);
        const r = await fetch(`/api/folders/${folderId}/upload`, {
            method: "POST",
            headers: { "X-Forwarded-Email": userEmail() },
            body: form,
        });
        if (!r.ok) throw new Error(`${r.status} upload: ${await r.text()}`);
        return r.json();
    },
    recentJobs: () => req("GET", "/api/jobs/recent?limit=50"),
    search: (query, modes = ["chunks"], folderIds = null, limit = 10) =>
        req("POST", "/api/search", { query, modes, folder_ids: folderIds, limit }),
};
