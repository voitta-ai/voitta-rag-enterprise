// WebSocket connection manager with backoff reconnect.

import { connStatus, folders, files, jobs, reindexProgress } from "./store.js";

const MAX_BACKOFF_MS = 30_000;
const TOPICS = ["folders", "files", "jobs", "stats"];

let socket = null;
let backoff = 500;

export function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    socket = new WebSocket(url);

    socket.addEventListener("open", () => {
        backoff = 500;
        connStatus.set("connected");
        socket.send(JSON.stringify({ type: "subscribe", topics: TOPICS }));
    });

    socket.addEventListener("message", (e) => {
        let frame;
        try { frame = JSON.parse(e.data); } catch { return; }
        // The server may send a single event or a batched frame
        // ({type: "batch", events: [...]}) when many events accumulated
        // since the last drain. Either way we route through handleEvent.
        if (frame && frame.type === "batch" && Array.isArray(frame.events)) {
            for (const ev of frame.events) handleEvent(ev);
        } else {
            handleEvent(frame);
        }
    });

    socket.addEventListener("close", () => {
        connStatus.set("disconnected");
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
    });

    socket.addEventListener("error", () => {
        socket.close();
    });
}

function handleEvent(event) {
    switch (event.type) {
        case "subscribed":
            return;
        case "folder.added":
            folders.update((list) => [...list.filter(f => f.id !== event.folder.id), event.folder]);
            return;
        case "folder.upserted":
            // Mutation that doesn't add or remove a folder — e.g. has_sync_source
            // toggled when the user saves or deletes a sync config.
            folders.update((list) => {
                const idx = list.findIndex(f => f.id === event.folder.id);
                if (idx === -1) return [...list, event.folder];
                const next = list.slice();
                next[idx] = { ...next[idx], ...event.folder };
                return next;
            });
            return;
        case "folder.removed":
            folders.update((list) => list.filter(f => f.id !== event.folder_id));
            files.update((list) => list.filter(f => f.folder_id !== event.folder_id));
            return;
        case "folder.reindex_progress":
            // Backend emits these in batches during a reindex job's
            // wipe / queue phases. Stash on the reindexProgress store so
            // the folder card can render a "Wiping… 600/1969" pill.
            // ``phase === "done"`` means the folder is no longer in the
            // wipe path — reset the entry so the pill disappears.
            reindexProgress.update((map) => {
                const next = new Map(map);
                if (event.phase === "done") {
                    next.delete(event.folder_id);
                } else {
                    next.set(event.folder_id, {
                        phase: event.phase,
                        done: event.done,
                        total: event.total,
                    });
                }
                return next;
            });
            return;
        case "file.upserted": {
            const f = event.file;
            files.update((list) => {
                const idx = list.findIndex(x => x.id === f.id);
                if (idx === -1) return [...list, f];
                const next = list.slice();
                next[idx] = { ...next[idx], ...f };
                return next;
            });
            return;
        }
        case "file.deleted":
            files.update((list) => list.filter(f => f.id !== event.file_id));
            return;
        case "job.started": {
            const j = { id: event.job_id, kind: event.kind, state: "running", payload: event.payload };
            jobs.update((list) => [j, ...list.filter(x => x.id !== j.id)].slice(0, 50));
            return;
        }
        case "job.finished": {
            jobs.update((list) => list.map(j =>
                j.id === event.job_id
                    ? { ...j, state: event.state, error: event.error || null }
                    : j,
            ));
            return;
        }
    }
}
