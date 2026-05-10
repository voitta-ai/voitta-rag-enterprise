"""Exporter registry + base-class invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voitta_rag_enterprise.services.sync.google_workspace_exporters import (
    ExportContext,
    ExporterRegistry,
    NativeDriveExporter,
    RemoteEntry,
    build_default_registry,
    get_default_registry,
    safe_filename,
)


# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("API: Endpoints", "API- Endpoints"),
        ("My / Document", "My - Document"),
        ("   spaced   out   ", "spaced out"),
        ("hello???world", "hello-world"),
        # Repeated dashes collapse.
        ("a----b", "a-b"),
        # Strips reserved chars only; non-reserved unicode passes through.
        ("résumé", "résumé"),
    ],
)
def test_safe_filename(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


def test_safe_filename_caps_at_80_chars() -> None:
    name = "x" * 200
    out = safe_filename(name)
    assert len(out) == 80
    assert out == "x" * 80


def test_safe_filename_uses_fallback_for_empty_input() -> None:
    assert safe_filename("", fallback="my-fallback") == "my-fallback"


def test_safe_filename_uses_fallback_when_only_reserved_chars() -> None:
    # ``///`` after sanitisation collapses to "-" then strips → empty.
    assert safe_filename("///", fallback="x") == "x"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class _FakeExporter(NativeDriveExporter):
    mime_type = "application/vnd.google-apps.fake"

    def export(
        self,
        item: dict[str, Any],
        rel_no_ext: str,  # noqa: ARG002
        ctx: ExportContext,  # noqa: ARG002
    ) -> list[RemoteEntry]:
        return [
            RemoteEntry(
                rel_path=f"{rel_no_ext}.fake",
                url="https://example/" + item["id"],
                fingerprint=item.get("modifiedTime", ""),
                producer=lambda dest, drive, ctx: None,
            )
        ]


def test_register_and_find_round_trips() -> None:
    r = ExporterRegistry()
    r.register(_FakeExporter())
    found = r.find("application/vnd.google-apps.fake")
    assert isinstance(found, _FakeExporter)


def test_find_returns_none_for_unknown_mime() -> None:
    r = ExporterRegistry()
    assert r.find("application/vnd.google-apps.unknown") is None


def test_register_replaces_existing_entry() -> None:
    """Test fixtures swap in stubs; replacement is intentional."""
    class _Replacement(_FakeExporter):
        pass

    r = ExporterRegistry()
    r.register(_FakeExporter())
    r.register(_Replacement())
    assert isinstance(r.find("application/vnd.google-apps.fake"), _Replacement)


def test_register_rejects_exporter_with_no_mime_type() -> None:
    class _Broken(NativeDriveExporter):
        # Inherits empty mime_type — registry must catch this.
        def export(self, item, rel_no_ext, ctx):  # noqa: ARG002
            return []

    r = ExporterRegistry()
    with pytest.raises(ValueError, match="mime_type"):
        r.register(_Broken())


def test_mime_types_lists_registered_handlers() -> None:
    r = ExporterRegistry()
    r.register(_FakeExporter())
    assert r.mime_types == ("application/vnd.google-apps.fake",)


def test_default_registry_is_cached() -> None:
    a = get_default_registry()
    b = get_default_registry()
    assert a is b


def test_build_default_registry_is_a_fresh_instance() -> None:
    """``get_default_registry`` is cached but ``build_default_registry`` is
    not — tests need a fresh registry per invocation to swap exporters
    safely."""
    a = build_default_registry()
    b = build_default_registry()
    assert a is not b


# ---------------------------------------------------------------------------
# RemoteEntry / contexts — shape
# ---------------------------------------------------------------------------


def test_remote_entry_defaults() -> None:
    entry = RemoteEntry(
        rel_path="foo.md",
        url="https://example",
        fingerprint="fp1",
        producer=lambda dest, drive, ctx: None,
    )
    assert entry.tab is None
    assert entry.size_hint is None
    assert entry.extra == {}


def test_export_context_carries_factories(tmp_path: Path) -> None:
    """ExportContext factories must be lazy: zero calls at construction."""
    calls: dict[str, int] = {"docs": 0, "sheets": 0}

    def _docs() -> Any:
        calls["docs"] += 1
        return object()

    def _sheets() -> Any:
        calls["sheets"] += 1
        return object()

    ctx = ExportContext(
        folder_root=tmp_path,
        docs=_docs,
        sheets=_sheets,
        slides=lambda: None,
        forms=lambda: None,
        drive_thread_local=lambda: None,
        access_token=None,
    )
    assert calls == {"docs": 0, "sheets": 0}
    ctx.docs()
    assert calls == {"docs": 1, "sheets": 0}
