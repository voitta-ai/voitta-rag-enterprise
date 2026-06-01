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
