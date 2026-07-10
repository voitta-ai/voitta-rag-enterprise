// Per-source-type handler table. Mirrors api/routes/sync/registry.py:
// each connector module registers one handler per source_type it owns;
// core.js dispatches through this instead of if/elif chains. Adding a
// connector = new module + one ``registerSource()`` call — core.js does
// not change.
//
// The registry deliberately imports nothing from its siblings, so it can
// never participate in a cycle.
//
// Handler shape (all methods optional unless noted):
//   type      server source_type string ("github", "google_drive",
//             "google_drive_local", "nfs", "sharepoint", "teams", "jira",
//             "confluence"). Required.
//   tab       the #sync-type dropdown value whose pane hosts this handler.
//             Same as ``type`` for all except google_drive_local, whose
//             "This Mac" tab lives under the "google_drive" entry. Required.
//   paneId    the #sync-form-* pane shown when ``tab`` is active. Absent on
//             google_drive_local (it shares google_drive's pane).
//   reset()   restore this connector's inputs to defaults (called by
//             openSyncModal). The microsoft pair shares ONE reset,
//             registered on the sharepoint handler, so the admin
//             provider-list call fires once for both connectors.
//   onShow()  called by setSyncType when this handler's tab becomes active
//             (nfs tree refresh, MS loopback hint, jira/cf auth-mode
//             defaults).
//   load(src) populate the form from the GET /sync payload when
//             ``src.source_type === type``. May be async (nfs awaits its
//             tree rebuild). Return ``true`` to skip the shared
//             auto-sync/status tail — google_drive_local renders its own
//             status line and has no shared footer.
//   formConfig()  return the per-source config object for PUT; absent for
//             google_drive_local (not PUT-able via the envelope).
//   afterSave(out)  post-save placeholder refresh (gd/ms/jira/cf).
//   beforeLoad()  promise that must settle before openSyncModal's
//             loadSyncSource pass runs (google_drive's provider-picker
//             refresh — the preselect-by-client_id pass needs a populated
//             provider map).
//   afterLoad()   runs after that loadSyncSource pass settles
//             (google_drive's single-provider auto-pick).
//   beforeSaveConfirm()  pre-save confirm hook; returns true when the user
//             asked to trigger a sync right after saving (google_drive's
//             removed-folders cleanup warning).
//   hidesChrome()  () => bool — true when this handler's active tab hides
//             the shared footer + Drive-folder selector + auto-sync row
//             (google_drive_local's "This Mac" tab: dropdown ==
//             "google_drive" AND the Drive sub-tab == "local").

export const SOURCES = new Map();
export function registerSource(h) { SOURCES.set(h.type, h); }
