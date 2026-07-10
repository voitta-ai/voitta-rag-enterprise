// Shared modal state for the sync package.
//
// ``ctx.folderId`` is the folder root whose sync config the modal is
// currently editing (was a module-level variable in the old single-file
// module). Every per-connector API call reads it, so it lives here — the
// one module anything may import without creating a cycle.

export const $ = (sel) => document.querySelector(sel);

// Default recency floor shown in the Jira/Confluence "modified since" date
// inputs for a NEW folder (existing folders show the server's stored/default
// value via loadJiraForm/loadConfluenceForm). Kept in step with the
// connectors' ISSUES_UPDATED_SINCE / PAGES_UPDATED_SINCE defaults.
export const SYNC_DEFAULT_SINCE = "2026-01-01";

export const ctx = { folderId: null };
