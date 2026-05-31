// Tiny reactive store. Each store is a value + a set of subscribers.
// Mutating the value through .set() notifies subscribers.

export function createStore(initial) {
    let value = initial;
    const subs = new Set();
    return {
        get: () => value,
        set: (next) => {
            value = next;
            for (const fn of subs) fn(value);
        },
        update: (fn) => {
            value = fn(value);
            for (const fn2 of subs) fn2(value);
        },
        subscribe: (fn) => {
            subs.add(fn);
            fn(value);
            return () => subs.delete(fn);
        },
    };
}

export const folders = createStore([]);   // [{id, path, display_name, ...}]
export const files = createStore([]);     // [{id, folder_id, rel_path, state, ...}]
export const jobs = createStore([]);      // [{id, kind, state, ...}]
export const connStatus = createStore("disconnected");
// Set<folder_id> — folders the backend currently has at least one
// queued/running job for. Seeded on bootstrap via
// ``GET /api/folders/active-ids`` and maintained from
// ``folder.active_changed`` WS events. Replaces the previous
// client-side derivation off the (truncated, 50-row) ``jobs`` store
// which mislabelled un-indexed folders as 'indexed' on deep queues.
// See services/folder_active.py for the server-side counter.
export const activeFolders = createStore(new Set());
// folder_id → { phase, done, total } while a reindex_folder job is mid-wipe.
// Empty map means "no folder is currently in a reindex wipe phase".
export const reindexProgress = createStore(new Map());
// Same shape, separate map: live progress for sync jobs (Drive listing,
// downloading, cleanup phases). Kept distinct from reindexProgress so the
// two can coexist on the same folder card without overwriting each other
// (e.g. user triggers sync, watcher fires reindex on extracted files).
export const syncProgress = createStore(new Map());
// folder_id → FolderStats payload (chunks_total, images_total, bytes_total,
// by_extension, index_health, …). Backend pushes ``folder.stats_changed``
// every time the indexer commits any artifact under the folder, so the
// sidebar reads from this store instead of polling /api/folders/{id}/stats.
// Empty map at boot; the SPA fetches the snapshot for the selected folder
// on first render so the panel doesn't flash empty values.
export const folderStats = createStore(new Map());
// folder_id → { sync_status, sync_error, last_synced_at }. Snapshot of the
// terminal fields on a folder's sync source row. Backend emits
// ``folder.sync_source_changed`` at each sync state transition (start,
// success, failure, error-cleared); the sync modal subscribes so an open
// modal updates the moment the status changes — no close+reopen needed,
// and edits in another tab propagate too. Distinct from ``syncProgress``
// (which carries the per-phase listing/downloading counters that come
// from the connector).
export const syncSources = createStore(new Map());
