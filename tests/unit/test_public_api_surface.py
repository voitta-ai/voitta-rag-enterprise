"""Public-import-surface contract for modules being split into packages.

Each list below is the set of names that external code (routes, mcp_server,
snapshot, watcher, main, and the test suite itself) imports from the module.
When a module becomes a package, its ``__init__.py`` must re-export every one
of these — this test catches a forgotten re-export at collection time instead
of when some lazy import fires in production.

Names here include private (``_``-prefixed) ones on purpose: they are imported
across module boundaries today, so they are part of the de-facto contract.
"""

from __future__ import annotations

import importlib

import pytest

SURFACES = {
    "voitta_rag_enterprise.services.indexing": [
        "HANDLERS",
        "run_extract",
        "run_embed_text",
        "run_embed_image",
        "run_sync",
        "run_delete_file",
        "run_reindex_folder",
        "reconcile_abandoned_extracts",
        "file_event_payload",
        "publish_file_upserted",
        "wipe_file_data",
        "_load_char_to_page",
        "_load_layout_summaries",
        "_decrement_pending_embeds",
        "_publish_job_progress",
        "_stage",
    ],
    "voitta_rag_enterprise.services.acl": [
        "ROOT_EMAIL",
        "CurrentUser",
        "resolve_user_email",
        "get_or_create_user",
        "accounts_for_email",
        "offered_accounts_for_email",
        "default_account_for_email",
        "person_is_admin",
        "stamp_person_admin",
        "seed_users_from_file",
        "public_user_ids",
        "account_community",
        "_owner_community",
        "grant_folder",
        "revoke_folder",
        "folder_user_ids",
        "visible_folder_ids",
        "mcp_visible_folder_ids",
        "user_can_see_folder",
        "is_folder_owner",
        "set_folder_active",
        "folder_active_for_user",
        "active_folder_ids",
        "user_can_see_file",
        "allowed_user_ids_for_file",
        "folder_user_id_email",
    ],
    "voitta_rag_enterprise.api.routes.sync": [
        "router",
        "oauth_router",
        "SyncSourceIn",
        "SyncSourceOut",
        "to_out",
        "_to_out",
    ],
    "voitta_rag_enterprise.api.routes.admin": [
        "router",
        "build_admin_state",
        "publish_admin_state",
    ],
    "voitta_rag_enterprise.api.routes.auth": [
        "router",
        # Compat re-exports after the api_keys split (Phase 1a) — external
        # code and tests import these from routes.auth today.
        "verify_token",
        "mint_token",
        "build_keys_state",
        "publish_keys_state",
    ],
}


@pytest.mark.parametrize("module_path", sorted(SURFACES))
def test_public_surface(module_path: str) -> None:
    mod = importlib.import_module(module_path)
    missing = [n for n in SURFACES[module_path] if not hasattr(mod, n)]
    assert not missing, f"{module_path} lost public names: {missing}"
