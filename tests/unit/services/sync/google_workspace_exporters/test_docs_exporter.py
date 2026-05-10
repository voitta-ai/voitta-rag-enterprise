"""DocumentExporter — tab markdown + inline image RemoteEntries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from voitta_rag_enterprise.services.sync.google_workspace_exporters import (
    ExportContext,
    ProducerContext,
    get_default_registry,
)
from voitta_rag_enterprise.services.sync.google_workspace_exporters.docs import (
    DocumentExporter,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDocsRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


class _FakeDocs:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def documents(self):
        return self

    def get(self, *, documentId: str, includeTabsContent: bool):  # noqa: ARG002
        return _FakeDocsRequest(self._payload)


def _ctx(tmp_path: Path, docs_payload: dict, access_token: str | None = "fake-tkn") -> ExportContext:
    return ExportContext(
        folder_root=tmp_path,
        docs=lambda: _FakeDocs(docs_payload),
        sheets=lambda: None,
        slides=lambda: None,
        forms=lambda: None,
        drive_thread_local=lambda: None,
        access_token=access_token,
    )


def _producer_ctx(tmp_path: Path) -> ProducerContext:
    return ProducerContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: None,
        slides=lambda: None,
        forms=lambda: None,
        access_token="fake-tkn",
    )


def _drive_item(item_id: str, name: str, modified_time: str = "2026-05-10T00:00:00Z") -> dict:
    return {
        "id": item_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": modified_time,
        "webViewLink": f"https://docs.google.com/document/d/{item_id}/edit",
    }


def _para(text: str) -> dict:
    return {
        "paragraph": {
            "elements": [{"textRun": {"content": text, "textStyle": {}}}]
        }
    }


def _tab(tab_id: str, title: str, content: list[dict]) -> dict:
    return {
        "tabProperties": {"tabId": tab_id, "title": title},
        "documentTab": {"body": {"content": content}},
    }


# ---------------------------------------------------------------------------
# MIME registration
# ---------------------------------------------------------------------------


def test_registry_dispatches_doc_mime() -> None:
    r = get_default_registry()
    found = r.find("application/vnd.google-apps.document")
    assert isinstance(found, DocumentExporter)


# ---------------------------------------------------------------------------
# Multi-tab → one md per tab
# ---------------------------------------------------------------------------


def test_multi_tab_doc_produces_one_md_per_tab(tmp_path: Path) -> None:
    payload = {
        "tabs": [
            _tab("t.intro", "Intro", [_para("welcome")]),
            _tab("t.api", "API", [_para("endpoints")]),
        ]
    }
    exporter = DocumentExporter()
    entries = exporter.export(_drive_item("doc1", "Specs"), "Specs", _ctx(tmp_path, payload))
    md_entries = [e for e in entries if e.rel_path.endswith(".md")]
    assert len(md_entries) == 2
    rel_paths = sorted(e.rel_path for e in md_entries)
    assert rel_paths == ["Specs/01-Intro.md", "Specs/02-API.md"]
    intro = next(e for e in md_entries if e.rel_path.endswith("Intro.md"))
    assert intro.tab == "Intro"
    assert "tab=t.intro" in intro.url
    assert intro.fingerprint == "2026-05-10T00:00:00Z#t.intro"


def test_tab_markdown_producer_writes_with_fingerprint_header(tmp_path: Path) -> None:
    payload = {"tabs": [_tab("t.0", "Only", [_para("body text")])]}
    entries = DocumentExporter().export(
        _drive_item("d", "Doc"), "Doc", _ctx(tmp_path, payload)
    )
    md = next(e for e in entries if e.rel_path.endswith(".md"))
    dest = tmp_path / md.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    md.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))
    text = dest.read_text(encoding="utf-8")
    first_line = text.splitlines()[0]
    assert first_line.startswith("<!--voitta-fingerprint:")
    assert md.fingerprint in first_line
    assert "body text" in text


# ---------------------------------------------------------------------------
# No-tabs (legacy) doc
# ---------------------------------------------------------------------------


def test_no_tabs_doc_renders_a_single_flat_md(tmp_path: Path) -> None:
    """An older doc returned without ``tabs`` lands as ``<stem>.md`` directly."""
    payload = {"body": {"content": [_para("legacy body")]}}
    entries = DocumentExporter().export(
        _drive_item("d2", "Plain"), "Plain", _ctx(tmp_path, payload)
    )
    md_entries = [e for e in entries if e.rel_path.endswith(".md")]
    assert len(md_entries) == 1
    assert md_entries[0].rel_path == "Plain.md"


# ---------------------------------------------------------------------------
# Inline images → image RemoteEntries
# ---------------------------------------------------------------------------


def test_inline_image_emits_image_remote_entry(tmp_path: Path) -> None:
    payload = {
        "tabs": [
            _tab(
                "t.0",
                "Only",
                [
                    {
                        "paragraph": {
                            "elements": [
                                {"inlineObjectElement": {"inlineObjectId": "kix.abc"}},
                            ]
                        }
                    }
                ],
            )
        ],
        "inlineObjects": {
            "kix.abc": {
                "embeddedObject": {
                    "imageProperties": {
                        "contentUri": "https://lh3.googleusercontent.com/abc",
                    }
                }
            }
        },
    }
    entries = DocumentExporter().export(
        _drive_item("d", "Doc"), "Doc", _ctx(tmp_path, payload)
    )
    images = [e for e in entries if e.rel_path.endswith(".png")]
    assert len(images) == 1
    img = images[0]
    assert img.rel_path == "Doc/01-Only/images/img_1.png"
    assert img.fingerprint.endswith("#kix.abc")


def test_image_producer_downloads_via_httpx_with_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _MockClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url: str, headers: dict[str, str]) -> httpx.Response:
            captured["url"] = url
            captured["headers"] = headers
            return httpx.Response(200, content=b"PNG-BYTES", request=httpx.Request("GET", url))

    monkeypatch.setattr(
        "voitta_rag_enterprise.services.sync.google_workspace_exporters.docs.httpx.Client",
        _MockClient,
    )

    payload = {
        "tabs": [
            _tab(
                "t.0",
                "Only",
                [
                    {
                        "paragraph": {
                            "elements": [
                                {"inlineObjectElement": {"inlineObjectId": "kix.abc"}},
                            ]
                        }
                    }
                ],
            )
        ],
        "inlineObjects": {
            "kix.abc": {
                "embeddedObject": {
                    "imageProperties": {"contentUri": "https://lh3.googleusercontent.com/abc"}
                }
            }
        },
    }
    entries = DocumentExporter().export(
        _drive_item("d", "Doc"), "Doc", _ctx(tmp_path, payload, access_token="bearer-1"),
    )
    img = next(e for e in entries if e.rel_path.endswith(".png"))
    dest = tmp_path / img.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))

    assert dest.read_bytes() == b"PNG-BYTES"
    assert captured["url"] == "https://lh3.googleusercontent.com/abc"
    assert captured["headers"]["Authorization"] == "Bearer bearer-1"


def test_inline_image_without_content_uri_is_skipped(tmp_path: Path) -> None:
    """Images Google can't surface bytes for (rare embed types) drop out
    silently — markdown still references the path but no producer fires."""
    payload = {
        "tabs": [
            _tab(
                "t.0",
                "Only",
                [
                    {
                        "paragraph": {
                            "elements": [
                                {"inlineObjectElement": {"inlineObjectId": "kix.x"}},
                            ]
                        }
                    }
                ],
            )
        ],
        "inlineObjects": {
            "kix.x": {"embeddedObject": {"imageProperties": {}}}  # no contentUri
        },
    }
    entries = DocumentExporter().export(
        _drive_item("d", "Doc"), "Doc", _ctx(tmp_path, payload)
    )
    assert all(not e.rel_path.endswith(".png") for e in entries)


def test_image_producer_raises_without_access_token(tmp_path: Path) -> None:
    """Hitting a contentUri without bearer auth fails with a 401; we
    short-circuit with a clearer error so the connector logs are
    actionable instead of pointing at httpx internals."""
    payload = {
        "tabs": [
            _tab(
                "t.0",
                "Only",
                [
                    {
                        "paragraph": {
                            "elements": [
                                {"inlineObjectElement": {"inlineObjectId": "kix.abc"}},
                            ]
                        }
                    }
                ],
            )
        ],
        "inlineObjects": {
            "kix.abc": {
                "embeddedObject": {
                    "imageProperties": {"contentUri": "https://lh3.googleusercontent.com/abc"}
                }
            }
        },
    }
    entries = DocumentExporter().export(
        _drive_item("d", "Doc"), "Doc", _ctx(tmp_path, payload, access_token=None),
    )
    img = next(e for e in entries if e.rel_path.endswith(".png"))
    dest = tmp_path / img.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="access token"):
        img.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))


def test_empty_tabs_array_returns_no_entries(tmp_path: Path) -> None:
    """A doc that reports tabs but renders nothing is a no-op — connector
    treats it as a successful empty file (not an error)."""
    payload = {"tabs": []}
    entries = DocumentExporter().export(
        _drive_item("d", "Doc"), "Doc", _ctx(tmp_path, payload)
    )
    assert entries == []
