"""Sync connector registry — dispatch + contract."""

from __future__ import annotations

import pytest

from voitta_rag_enterprise.services.sync import (
    SyncConnector,
    get_connector,
    get_registry,
)

EXPECTED_TYPES = {"github", "google_drive", "nfs", "sharepoint", "teams"}


def test_registry_lists_all_connectors() -> None:
    assert set(get_registry().list_types()) == EXPECTED_TYPES


@pytest.mark.parametrize("source_type", sorted(EXPECTED_TYPES))
def test_get_connector_resolves_each_type(source_type: str) -> None:
    c = get_connector(source_type)
    assert isinstance(c, SyncConnector)
    assert c.source_type == source_type
    # supports_progress mirrors which connectors run_sync threads a callback to.
    assert isinstance(c.supports_progress, bool)


def test_only_github_lacks_progress() -> None:
    progressless = {
        t for t in EXPECTED_TYPES if not get_connector(t).supports_progress
    }
    assert progressless == {"github"}


def test_unknown_source_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown sync source_type"):
        get_connector("nope")


def _blank_row():
    """A duck-typed FolderSyncSource with every column defaulted falsy.

    ``resolve_config`` only reads attributes, so a SimpleNamespace stands in
    without a DB session — enough to prove each connector builds its kwargs
    from the row instead of the old per-type switchboard in run_sync.
    """
    from types import SimpleNamespace

    cols = (
        "gh_repo gh_path gh_branches gh_all_branches gh_extended gh_auth_method "
        "gh_username gh_pat gh_token "
        "gd_folder_id gd_files_only gd_client_id gd_client_secret gd_refresh_token "
        "gd_service_account_json "
        "nfs_subpaths nfs_subpath "
        "ms_tenant_id ms_client_id ms_client_secret ms_cert_pem ms_refresh_token "
        "ms_auth_method sp_selected_sites sp_all_sites "
        "tm_user_mode tm_user_id tm_include_attended"
    ).split()
    return SimpleNamespace(**{c: None for c in cols})


@pytest.mark.parametrize(
    ("source_type", "must_have_keys"),
    [
        ("github", {"repo_url", "branches", "auth"}),
        ("google_drive", {"drive_folders", "files_only", "auth"}),
        ("nfs", {"nfs_subpaths"}),
        ("sharepoint", {"auth", "sites", "all_sites"}),
        ("teams", {"auth", "user_mode", "user_id", "include_attended"}),
    ],
)
def test_resolve_config_builds_kwargs_from_row(source_type, must_have_keys) -> None:
    cfg = get_connector(source_type).resolve_config(_blank_row())
    assert must_have_keys <= set(cfg)
    # progress_cb is added by run_sync, never by the connector.
    assert "progress_cb" not in cfg
