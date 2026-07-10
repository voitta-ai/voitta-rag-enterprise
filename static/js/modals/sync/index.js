// Configure-sync modal package — entry point.
//
// core.js first: it owns the modal chrome, the generic dispatchers and the
// shared footer wiring. Then each connector module registers its
// source_type handler(s) in registry.js as an import side effect.
// (google_drive.js imports google_local.js, so google_drive_local
// registers just before google_drive — registration order only affects
// the reset loop, whose steps are independent.)
import "./core.js";
import "./github.js";
import "./google_drive.js";
import "./google_local.js";
import "./nfs.js";
import "./microsoft.js";
import "./jira.js";
import "./confluence.js";
