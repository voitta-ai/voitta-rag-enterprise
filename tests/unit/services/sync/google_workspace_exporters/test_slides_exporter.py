"""PresentationExporter — per-slide md + thumbnails + speaker notes."""

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
from voitta_rag_enterprise.services.sync.google_workspace_exporters.slides import (
    PresentationExporter,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


class _FakeSlides:
    def __init__(self, deck: dict, thumbnails: dict[tuple[str, str], dict]) -> None:
        self._deck = deck
        self._thumbnails = thumbnails

    def presentations(self):
        outer = self

        class _Presentations:
            def get(self, *, presentationId: str) -> _FakeRequest:  # noqa: ARG002
                return _FakeRequest(outer._deck)

            def pages(self):
                class _Pages:
                    def getThumbnail(
                        self,
                        *,
                        presentationId: str,
                        pageObjectId: str,
                        thumbnailProperties_thumbnailSize: str = "",  # noqa: ARG002
                    ) -> _FakeRequest:
                        return _FakeRequest(outer._thumbnails[(presentationId, pageObjectId)])

                return _Pages()

        return _Presentations()


def _ctx(
    tmp_path: Path,
    *,
    deck: dict,
    thumbnails: dict[tuple[str, str], dict] | None = None,
) -> ExportContext:
    return ExportContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: None,
        slides=lambda: _FakeSlides(deck, thumbnails or {}),
        forms=lambda: None,
        drive_thread_local=lambda: None,
        access_token="fake",
    )


def _producer_ctx(
    tmp_path: Path,
    *,
    deck: dict | None = None,
    thumbnails: dict[tuple[str, str], dict] | None = None,
) -> ProducerContext:
    slides = _FakeSlides(deck or {}, thumbnails or {})
    return ProducerContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: None,
        slides=lambda: slides,
        forms=lambda: None,
        access_token="fake",
    )


def _drive_item(
    item_id: str, name: str, modified_time: str = "2026-05-10T00:00:00Z"
) -> dict:
    return {
        "id": item_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.presentation",
        "modifiedTime": modified_time,
        "webViewLink": f"https://docs.google.com/presentation/d/{item_id}/edit",
    }


def _shape(text: str, *, placeholder: str | None = None) -> dict:
    el: dict = {
        "shape": {
            "text": {
                "textElements": [{"textRun": {"content": text}}]
            }
        }
    }
    if placeholder is not None:
        el["shape"]["placeholder"] = {"type": placeholder}
    return el


def _slide(
    object_id: str,
    page_elements: list[dict],
    notes_elements: list[dict] | None = None,
) -> dict:
    out: dict = {"objectId": object_id, "pageElements": page_elements}
    if notes_elements is not None:
        out["slideProperties"] = {
            "notesPage": {"pageElements": notes_elements}
        }
    return out


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registry_dispatches_slides_mime() -> None:
    r = get_default_registry()
    found = r.find("application/vnd.google-apps.presentation")
    assert isinstance(found, PresentationExporter)


# ---------------------------------------------------------------------------
# Slide markdown shape
# ---------------------------------------------------------------------------


def test_export_emits_md_and_thumbnail_per_slide(tmp_path: Path) -> None:
    deck = {
        "slides": [
            _slide("p1", [_shape("Intro", placeholder="TITLE"), _shape("Welcome.")]),
            _slide("p2", [_shape("Roadmap", placeholder="TITLE"), _shape("Q3 themes.")]),
        ]
    }
    entries = PresentationExporter().export(
        _drive_item("deck1", "Pitch"),
        "Pitch",
        _ctx(tmp_path, deck=deck),
    )
    md_paths = sorted(e.rel_path for e in entries if e.rel_path.endswith(".md"))
    img_paths = sorted(e.rel_path for e in entries if e.rel_path.endswith(".png"))
    assert md_paths == ["Pitch/01-Intro.md", "Pitch/02-Roadmap.md"]
    assert img_paths == ["Pitch/images/slide_1.png", "Pitch/images/slide_2.png"]


def test_slide_url_carries_slide_anchor(tmp_path: Path) -> None:
    deck = {
        "slides": [
            _slide("p1", [_shape("Intro", placeholder="TITLE")]),
        ]
    }
    entry = PresentationExporter().export(
        _drive_item("deck1", "Pitch"),
        "Pitch",
        _ctx(tmp_path, deck=deck),
    )[0]
    assert entry.url.endswith("#slide=id.p1")


def test_md_contains_title_body_and_image_reference(tmp_path: Path) -> None:
    deck = {
        "slides": [
            _slide(
                "p1",
                [
                    _shape("Intro", placeholder="TITLE"),
                    _shape("Body para 1."),
                    _shape("Body para 2."),
                ],
            )
        ]
    }
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck),
        )
        if e.rel_path.endswith(".md")
    )
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))
    text = dest.read_text(encoding="utf-8")
    assert "# Slide 1: Intro" in text
    assert "![](images/slide_1.png)" in text
    assert "Body para 1." in text
    assert "Body para 2." in text


def test_speaker_notes_are_quoted(tmp_path: Path) -> None:
    deck = {
        "slides": [
            _slide(
                "p1",
                [_shape("Intro", placeholder="TITLE"), _shape("Body.")],
                notes_elements=[_shape("Tell the story slowly.\nDouble-take pause.")],
            )
        ]
    }
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck),
        )
        if e.rel_path.endswith(".md")
    )
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))
    text = dest.read_text(encoding="utf-8")
    assert "> Speaker notes:" in text
    assert "> Tell the story slowly." in text
    assert "> Double-take pause." in text


def test_no_speaker_notes_block_when_notes_empty(tmp_path: Path) -> None:
    deck = {
        "slides": [
            _slide("p1", [_shape("Intro", placeholder="TITLE")], notes_elements=[]),
        ]
    }
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck),
        )
        if e.rel_path.endswith(".md")
    )
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))
    text = dest.read_text(encoding="utf-8")
    assert "Speaker notes" not in text


def test_slide_without_title_falls_back_to_index_label(tmp_path: Path) -> None:
    deck = {"slides": [_slide("p1", [_shape("Just a body bullet.")])]}
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck),
        )
        if e.rel_path.endswith(".md")
    )
    # No title placeholder → display title is the synthesised "Slide 1".
    assert entry.rel_path == "Pitch/01-Slide 1.md"
    assert entry.tab == "Slide 1"


def test_table_cells_contribute_to_body_text(tmp_path: Path) -> None:
    """Slides commonly hold bullet content inside tables; we extract it."""
    deck = {
        "slides": [
            _slide(
                "p1",
                [
                    _shape("Title", placeholder="TITLE"),
                    {
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {
                                            "text": {
                                                "textElements": [
                                                    {"textRun": {"content": "row1col1"}}
                                                ]
                                            }
                                        },
                                        {
                                            "text": {
                                                "textElements": [
                                                    {"textRun": {"content": "row1col2"}}
                                                ]
                                            }
                                        },
                                    ]
                                }
                            ]
                        }
                    },
                ],
            )
        ]
    }
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck),
        )
        if e.rel_path.endswith(".md")
    )
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))
    text = dest.read_text(encoding="utf-8")
    assert "row1col1" in text
    assert "row1col2" in text


def test_vertical_tab_inside_run_becomes_newline(tmp_path: Path) -> None:
    """Slides emit U+000B for shift-Enter; we normalise so paragraph
    splits survive."""
    deck = {
        "slides": [
            _slide(
                "p1",
                [
                    _shape("T", placeholder="TITLE"),
                    {
                        "shape": {
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "line1\vline2"}}
                                ]
                            }
                        }
                    },
                ],
            )
        ]
    }
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck),
        )
        if e.rel_path.endswith(".md")
    )
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(dest, drive=None, ctx=_producer_ctx(tmp_path))
    text = dest.read_text(encoding="utf-8")
    assert "line1\nline2" in text


def test_empty_deck_returns_no_entries(tmp_path: Path) -> None:
    entries = PresentationExporter().export(
        _drive_item("deck1", "Pitch"),
        "Pitch",
        _ctx(tmp_path, deck={"slides": []}),
    )
    assert entries == []


# ---------------------------------------------------------------------------
# Thumbnail producer
# ---------------------------------------------------------------------------


def test_thumbnail_producer_fetches_content_url(
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

        def get(self, url: str) -> httpx.Response:
            captured["url"] = url
            return httpx.Response(
                200, content=b"PNG-BYTES", request=httpx.Request("GET", url)
            )

    monkeypatch.setattr(
        "voitta_rag_enterprise.services.sync.google_workspace_exporters.slides.httpx.Client",
        _MockClient,
    )

    deck = {"slides": [_slide("p1", [_shape("T", placeholder="TITLE")])]}
    thumbnails = {
        ("deck1", "p1"): {"contentUrl": "https://lh3.googleusercontent.com/thumbnail-p1"}
    }
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck, thumbnails=thumbnails),
        )
        if e.rel_path.endswith(".png")
    )
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(
        dest,
        drive=None,
        ctx=_producer_ctx(tmp_path, deck=deck, thumbnails=thumbnails),
    )
    assert dest.read_bytes() == b"PNG-BYTES"
    assert captured["url"] == "https://lh3.googleusercontent.com/thumbnail-p1"


def test_thumbnail_producer_raises_without_content_url(tmp_path: Path) -> None:
    deck = {"slides": [_slide("p1", [_shape("T", placeholder="TITLE")])]}
    thumbnails = {("deck1", "p1"): {}}  # no contentUrl
    entry = next(
        e
        for e in PresentationExporter().export(
            _drive_item("deck1", "Pitch"),
            "Pitch",
            _ctx(tmp_path, deck=deck, thumbnails=thumbnails),
        )
        if e.rel_path.endswith(".png")
    )
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="contentUrl"):
        entry.producer(
            dest,
            drive=None,
            ctx=_producer_ctx(tmp_path, deck=deck, thumbnails=thumbnails),
        )
