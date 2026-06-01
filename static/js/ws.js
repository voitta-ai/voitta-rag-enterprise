// WebSocket connection manager with backoff reconnect.

import { activeFolders, adminState, connStatus, folders, files, jobs, keysState, reindexProgress, syncConfigs, syncProgress, syncSources, folderStats } from "./store.js";

const MAX_BACKOFF_MS = 30_000;
// ``admin`` and ``keys`` are subscribed by everyone; the server only delivers
// admin.* to admins and keys.* to the owning user, so non-recipients just get
// nothing on those planes.
const TOPICS = ["folders", "files", "jobs", "stats", "admin", "keys"];

// Application close code the server uses for an unauthenticated handshake.
// We stop reconnecting and bounce to login rather than hammering /ws.
const WS_CLOSE_UNAUTHENTICATED = 4401;

let socket = null;
let backoff = 500;
let stopped = false;

export function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    socket = new WebSocket(url);

    socket.addEventListener("open", () => {
        backoff = 500;
        // "syncing" until the server's snapshot + ``synced`` sentinel land.
        // On a reconnect the stores still hold the (possibly stale) previous
        // state during this window; the snapshot replaces them wholesale.
        connStatus.set("syncing");
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

    socket.addEventListener("close", (e) => {
        if (e.code === WS_CLOSE_UNAUTHENTICATED) {
            // Session expired / not signed in — reconnecting won't help.
            stopped = true;
            connStatus.set("unauthenticated");
            // Reload so the app's auth gate (ensureAuthenticated) takes over
            // and routes the user to sign-in.
            location.reload();
            return;
        }
        connStatus.set("disconnected");
        if (stopped) return;
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
    });

    socket.addEventListener("error", () => {
        socket.close();
    });
}

// Apply a full-state snapshot frame by REPLACING the store wholesale, so
// anything deleted while the socket was down disappears. Deltas that arrive
// afterwards merge on top. This is the mechanism that makes reconnect
// bulletproof: every (re)connect re-snapshots and the client converges to
// server truth without a page reload.
function applySnapshot(topic, items) {
    switch (topic) {
        case "folders":
            folders.set(items);
            return;
        case "active":
            activeFolders.set(new Set(items));
            return;
        case "files":
            files.set(items);
            return;
        case "jobs":
            jobs.set(items);
            return;
    }
}

function handleEvent(event) {
    switch (event.type) {
        case "subscribed":
            return;
        case "snapshot":
            applySnapshot(event.topic, event.items || []);
            return;
        case "synced":
            // Baseline delivered; live deltas follow. The pill goes green.
            connStatus.set("connected");
            return;
        case "admin.snapshot":
            // Full admin-console state (connect snapshot + every admin
            // mutation). Replace wholesale — the admin modal renders from it.
            adminState.set(event.state);
            return;
        case "keys.snapshot":
            // The signed-in user's API keys; replace wholesale.
            keysState.set(event.items || []);
            return;
        case "folder.sync_config_changed":
            // Per-folder connector config (secret-masked). null = deleted.
            syncConfigs.update((map) => {
                const next = new Map(map);
                if (event.config === null) next.delete(event.folder_id);
                else next.set(event.folder_id, event.config);
                return next;
            });
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
            folderStats.update((map) => {
                if (!map.has(event.folder_id)) return map;
                const next = new Map(map);
                next.delete(event.folder_id);
                return next;
            });
            return;
        case "folder.stats_changed":
            // Per-folder snapshot. Backend coalesces by folder_id so a
            // 200-file extract burst delivers one event with the freshest
            // counts, not 200. We swap a fresh Map so the subscriber's
            // identity check fires.
            folderStats.update((map) => {
                const next = new Map(map);
                next.set(event.folder_id, event.stats);
                return next;
            });
            return;
        case "folder.active_changed":
            // Server-pushed signal: this folder now has (or no longer
            // has) at least one queued/running job. Drives the
            // "indexing" pill across the tree, replacing the previous
            // client-side derivation off the 50-row ``jobs`` window.
            // ``folder.active_changed`` events are coalesced per
            // folder_id by services/events.py, so the SPA only ever
            // sees the latest boolean for each folder. Swap to a fresh
            // Set so identity-based subscribers re-fire.
            activeFolders.update((set) => {
                const next = new Set(set);
                if (event.active) next.add(event.folder_id);
                else next.delete(event.folder_id);
                return next;
            });
            return;
        case "folder.sync_source_changed":
            // Terminal sync-source fields (status, error, last_synced_at).
            // Backend coalesces by folder_id; we mirror the same shape on
            // the client so the sync modal can re-render its status line
            // in-place without a REST refetch.
            syncSources.update((map) => {
                const next = new Map(map);
                next.set(event.folder_id, {
                    sync_status: event.sync_status,
                    sync_error: event.sync_error,
                    last_synced_at: event.last_synced_at,
                });
                return next;
            });
            return;
        case "folder.sync_progress":
            // Backend connector + worker emit these during the listing /
            // downloading / cleaning phases of a sync job. Same shape as
            // reindex_progress; kept on a separate store so they don't
            // overwrite each other (a folder can be syncing AND mid-reindex
            // — they're independent phases). ``detail`` carries phase-
            // specific breadcrumbs (current_folder, items_seen) the badge
            // renders for movement during long enumerations.
            syncProgress.update((map) => {
                const next = new Map(map);
                if (event.phase === "done") {
                    next.delete(event.folder_id);
                } else {
                    next.set(event.folder_id, {
                        phase: event.phase,
                        done: event.done,
                        total: event.total,
                        detail: event.detail || null,
                    });
                }
                return next;
            });
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
                        // ``detail`` carries free-form context for a phase.
                        // For phase='queued' the REST handler stashes
                        // ``{behind: rel_path}`` so the SPA can render
                        // "Queued behind big.pdf" instead of a bare pill.
                        detail: event.detail || null,
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
            // started_at_ms is a client-side stamp used by the Jobs panel
            // to render a live "running 12s" suffix on the status pill.
            // The server doesn't ship the claim time on this event, but
            // we know we observed the start within ws transit so the
            // client clock is a tight enough proxy for a human counter.
            const j = {
                id: event.job_id,
                kind: event.kind,
                state: "running",
                payload: event.payload,
                display_path: event.display_path || null,
                started_at_ms: Date.now(),
            };
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
