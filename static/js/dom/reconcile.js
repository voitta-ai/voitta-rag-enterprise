// Pure DOM utilities for keyed reconciliation.
//
// Three primitives shared by every list-rendering module:
// - ``setIfChanged``: idempotent property write so the browser doesn't
//   invalidate style for an unchanged value.
// - ``reconcileChildren``: align a parent element's child order with
//   ``targetRows`` using minimum DOM moves. A row already at the right
//   position stays put — its :hover state and any in-flight click
//   survive intact.
// - Cache pruning: callers pass the cache Map so we can drop entries
//   the latest render didn't touch. Keeps the cache tracking the live
//   DOM exactly without forcing each caller to repeat the pattern.
//
// No store imports, no module-level state — ESM-friendly leaf utility.

export function setIfChanged(el, prop, value) {
    if (el[prop] !== value) el[prop] = value;
}

export function reconcileChildren(parent, targetRows, seenKeys, cache) {
    let cursor = parent.firstChild;
    for (const node of targetRows) {
        if (node === cursor) {
            cursor = cursor.nextSibling;
        } else {
            parent.insertBefore(node, cursor);
            // ``node`` is now where ``cursor`` was; cursor still points
            // at the same logical "next" element (which moved one slot
            // forward), so we don't advance it here.
        }
    }
    while (cursor) {
        const next = cursor.nextSibling;
        cursor.remove();
        cursor = next;
    }
    for (const k of [...cache.keys()]) {
        if (!seenKeys.has(k)) cache.delete(k);
    }
}
