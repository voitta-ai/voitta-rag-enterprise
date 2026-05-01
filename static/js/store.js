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
// folder_id → { phase, done, total } while a reindex_folder job is mid-wipe.
// Empty map means "no folder is currently in a reindex wipe phase".
export const reindexProgress = createStore(new Map());
