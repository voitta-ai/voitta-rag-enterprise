"""Tests for the sync-connector registry scaffold."""

from __future__ import annotations

import pytest

from voitta_rag_enterprise.services.sources import (
    FILESYSTEM_SOURCE,
    SourceRegistry,
    SyncConnector,
    SyncResult,
    get_default_registry,
    reset_registry_cache,
)


class _StubConnector:
    source_type = "stub"

    def validate_config(self, config: dict) -> None:
        if "url" not in config:
            raise ValueError("url required")

    async def sync(self, dest_dir, config):
        return SyncResult(source_type=self.source_type, files_added=1)


def test_register_and_lookup() -> None:
    r = SourceRegistry()
    c = _StubConnector()
    r.register(c)
    assert r.get("stub") is c
    assert r.list_types() == ["stub"]


def test_register_filesystem_rejected() -> None:
    r = SourceRegistry()

    class BadConnector:
        source_type = FILESYSTEM_SOURCE

        def validate_config(self, c): pass
        async def sync(self, d, c): return SyncResult(source_type=FILESYSTEM_SOURCE)

    with pytest.raises(ValueError, match="reserved"):
        r.register(BadConnector())


def test_register_duplicate_rejected() -> None:
    r = SourceRegistry()
    r.register(_StubConnector())
    with pytest.raises(ValueError, match="Duplicate"):
        r.register(_StubConnector())


def test_get_unknown_returns_none() -> None:
    r = SourceRegistry()
    assert r.get("nope") is None


def test_default_registry_is_cached() -> None:
    reset_registry_cache()
    a = get_default_registry()
    b = get_default_registry()
    assert a is b
    reset_registry_cache()
    assert get_default_registry() is not a


def test_sync_result_changed_count() -> None:
    r = SyncResult(source_type="x", files_added=1, files_updated=2, files_removed=3)
    assert r.changed == 6


def test_protocol_compatible() -> None:
    """Confirm a class with the expected attributes satisfies SyncConnector."""
    c: SyncConnector = _StubConnector()
    assert c.source_type == "stub"
