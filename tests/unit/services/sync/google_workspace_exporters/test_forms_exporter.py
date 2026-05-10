"""FormExporter — Forms API → text-only markdown."""

from __future__ import annotations

from pathlib import Path

from voitta_rag_enterprise.services.sync.google_workspace_exporters import (
    ExportContext,
    ProducerContext,
    get_default_registry,
)
from voitta_rag_enterprise.services.sync.google_workspace_exporters.forms import (
    FormExporter,
    render_form_markdown,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


class _FakeForms:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def forms(self):
        return self

    def get(self, *, formId: str) -> _FakeRequest:  # noqa: ARG002
        return _FakeRequest(self._payload)


def _ctx(tmp_path: Path, payload: dict) -> ExportContext:
    return ExportContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: None,
        slides=lambda: None,
        forms=lambda: _FakeForms(payload),
        drive_thread_local=lambda: None,
        access_token="fake",
    )


def _producer_ctx(tmp_path: Path, payload: dict) -> ProducerContext:
    return ProducerContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: None,
        slides=lambda: None,
        forms=lambda: _FakeForms(payload),
        access_token="fake",
    )


def _drive_item(item_id: str, name: str) -> dict:
    return {
        "id": item_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.form",
        "modifiedTime": "2026-05-10T00:00:00Z",
        "webViewLink": f"https://docs.google.com/forms/d/{item_id}/edit",
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registry_dispatches_form_mime() -> None:
    r = get_default_registry()
    found = r.find("application/vnd.google-apps.form")
    assert isinstance(found, FormExporter)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_form_markdown_title_and_description() -> None:
    md = render_form_markdown(
        {"info": {"title": "Onboarding survey", "description": "Help us improve."}}
    )
    assert "# Onboarding survey" in md
    assert "Help us improve." in md


def test_render_form_markdown_section_break() -> None:
    md = render_form_markdown(
        {
            "info": {"title": "Survey"},
            "items": [
                {
                    "title": "About you",
                    "description": "Demographics",
                    "pageBreakItem": {},
                }
            ],
        }
    )
    assert "## About you" in md
    assert "Demographics" in md


def test_render_form_markdown_radio_question_with_options() -> None:
    md = render_form_markdown(
        {
            "info": {"title": "Survey"},
            "items": [
                {
                    "title": "Favourite colour?",
                    "questionItem": {
                        "question": {
                            "required": True,
                            "choiceQuestion": {
                                "type": "RADIO",
                                "options": [
                                    {"value": "Red"},
                                    {"value": "Blue"},
                                    {"isOther": True},
                                ],
                            },
                        }
                    },
                }
            ],
        }
    )
    assert "### Favourite colour?" in md
    assert "*[required]*" in md
    assert "_(radio choice)_" in md
    assert "- Red" in md
    assert "- Blue" in md
    # ``isOther`` becomes the literal "Other…" label.
    assert "- Other…" in md


def test_render_form_markdown_text_question() -> None:
    md = render_form_markdown(
        {
            "info": {"title": "Survey"},
            "items": [
                {
                    "title": "Tell us why",
                    "questionItem": {
                        "question": {
                            "textQuestion": {"paragraph": True},
                        }
                    },
                }
            ],
        }
    )
    assert "### Tell us why" in md
    assert "_(long-answer text)_" in md
    # Long-answer questions don't list any options.
    assert "- " not in md.split("Tell us why", 1)[1]


def test_render_form_markdown_scale_question() -> None:
    md = render_form_markdown(
        {
            "info": {"title": "Survey"},
            "items": [
                {
                    "title": "How satisfied?",
                    "questionItem": {
                        "question": {
                            "scaleQuestion": {
                                "low": 1,
                                "high": 5,
                                "lowLabel": "Not at all",
                                "highLabel": "Loved it",
                            }
                        }
                    },
                }
            ],
        }
    )
    assert "### How satisfied?" in md
    assert "_(scale)_" in md
    assert "- Scale: 1 (Not at all) → 5 (Loved it)" in md


def test_render_form_markdown_question_group_grid() -> None:
    md = render_form_markdown(
        {
            "info": {"title": "Survey"},
            "items": [
                {
                    "title": "Rate features",
                    "questionGroupItem": {
                        "questions": [
                            {"rowQuestion": {"title": "Speed"}},
                            {"rowQuestion": {"title": "Reliability"}},
                        ],
                        "grid": {
                            "columns": {
                                "options": [
                                    {"value": "Bad"},
                                    {"value": "Good"},
                                ]
                            }
                        },
                    },
                }
            ],
        }
    )
    assert "### Rate features" in md
    assert "#### Speed" in md
    assert "#### Reliability" in md
    assert "- Bad" in md
    assert "- Good" in md


def test_render_form_markdown_text_item_passthrough() -> None:
    md = render_form_markdown(
        {
            "info": {"title": "Survey"},
            "items": [
                {
                    "title": "Note",
                    "description": "Read carefully.",
                    "textItem": {},
                }
            ],
        }
    )
    assert "### Note" in md
    assert "Read carefully." in md


def test_render_form_markdown_image_item_renders_title_only() -> None:
    md = render_form_markdown(
        {
            "info": {"title": "Survey"},
            "items": [
                {"title": "See the diagram", "imageItem": {}},
            ],
        }
    )
    assert "### See the diagram" in md


def test_render_form_markdown_empty_form() -> None:
    md = render_form_markdown({})
    assert md == "# Untitled form"


# ---------------------------------------------------------------------------
# Exporter / producer
# ---------------------------------------------------------------------------


def test_export_returns_one_md_entry(tmp_path: Path) -> None:
    payload = {"info": {"title": "Survey"}}
    entries = FormExporter().export(
        _drive_item("f1", "Survey"),
        "Survey",
        _ctx(tmp_path, payload),
    )
    assert len(entries) == 1
    assert entries[0].rel_path == "Survey.md"
    assert entries[0].fingerprint == "2026-05-10T00:00:00Z"


def test_form_producer_writes_markdown_with_fingerprint_header(tmp_path: Path) -> None:
    payload = {
        "info": {"title": "Onboarding"},
        "items": [
            {
                "title": "Q1",
                "questionItem": {
                    "question": {
                        "choiceQuestion": {
                            "type": "RADIO",
                            "options": [{"value": "A"}, {"value": "B"}],
                        }
                    }
                },
            }
        ],
    }
    entry = FormExporter().export(
        _drive_item("f1", "Onboarding"),
        "Onboarding",
        _ctx(tmp_path, payload),
    )[0]
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(dest, drive=None, ctx=_producer_ctx(tmp_path, payload))
    text = dest.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("<!--voitta-fingerprint:")
    assert "# Onboarding" in text
    assert "### Q1" in text
    assert "- A" in text and "- B" in text
