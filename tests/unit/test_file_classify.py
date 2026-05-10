"""Tests for :mod:`services.file_classify` — the single source of truth
for "what kind of file is this".

Two surfaces:
* :func:`source_kind` — machine string surfaced on the MCP wire.
* :func:`bucket_label` — display label used by the sidebar.

Both consult ``source_url`` first then fall back to the extension, and
they must stay in lockstep. New Workspace types added to the
``_WORKSPACE_BUCKETS`` tuple should automatically be reflected here.
"""

from __future__ import annotations

from voitta_rag_enterprise.db.models import File
from voitta_rag_enterprise.services.file_classify import (
    bucket_label,
    source_kind,
)


def _f(rel_path: str, source_url: str | None = None) -> File:
    """Build a transient File row (not persisted) just for classification.

    The classifier reads two attributes — extension via Path(rel_path)
    and the source_url string — so a detached object is enough.
    """
    return File(
        folder_id=1,
        rel_path=rel_path,
        size_bytes=10,
        mtime_ns=0,
        last_seen_at=0,
        state="indexed",
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Workspace classification by source_url
# ---------------------------------------------------------------------------


def test_google_doc_classified_via_source_url() -> None:
    f = _f("Proj/01-Intro.md", "https://docs.google.com/document/d/abc/edit")
    assert source_kind(f) == "google_doc"
    assert bucket_label(f) == "Google Doc"


def test_google_sheet_classified_via_source_url() -> None:
    f = _f("Q3/01-Sales.md", "https://docs.google.com/spreadsheets/d/x/edit")
    assert source_kind(f) == "google_sheet"
    assert bucket_label(f) == "Google Sheet"


def test_google_slides_classified_via_source_url() -> None:
    f = _f("Pitch/01-Slide.md", "https://docs.google.com/presentation/d/x/edit")
    assert source_kind(f) == "google_slides"
    assert bucket_label(f) == "Google Slides"


def test_google_form_classified_via_source_url() -> None:
    f = _f("Surveys/Feedback.md", "https://docs.google.com/forms/d/x/edit")
    assert source_kind(f) == "google_form"
    assert bucket_label(f) == "Google Form"


def test_google_drawing_classified_via_source_url() -> None:
    f = _f("Diagrams/01.md", "https://docs.google.com/drawings/d/x/edit")
    assert source_kind(f) == "google_drawing"
    assert bucket_label(f) == "Google Drawing"


def test_url_with_tab_query_still_matches_prefix() -> None:
    """A Google Doc with a per-tab anchor still matches; the prefix
    check only looks at the start of the URL, not exact equality."""
    f = _f("a.md", "https://docs.google.com/document/d/abc/edit?tab=t.1")
    assert source_kind(f) == "google_doc"


# ---------------------------------------------------------------------------
# Extension fallback
# ---------------------------------------------------------------------------


def test_pdf_classified_via_extension() -> None:
    f = _f("report.pdf")
    assert source_kind(f) == "pdf"
    assert bucket_label(f) == ".pdf"


def test_docx_xlsx_pptx_classified_via_extension() -> None:
    for ext, kind in [(".docx", "docx"), (".xlsx", "xlsx"), (".pptx", "pptx")]:
        f = _f(f"x{ext}")
        assert source_kind(f) == kind
        assert bucket_label(f) == ext


def test_markdown_with_no_source_url_stays_markdown() -> None:
    """A vanilla README must not get pulled into a Workspace bucket."""
    f = _f("README.md")
    assert source_kind(f) == "markdown"
    assert bucket_label(f) == ".md"


def test_image_extensions_grouped_under_image_kind() -> None:
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
        f = _f(f"img{ext}")
        assert source_kind(f) == "image"


def test_unknown_extension_falls_back_to_other() -> None:
    f = _f("blob.weirdext")
    assert source_kind(f) == "other"
    assert bucket_label(f) == ".weirdext"


def test_extensionless_file_buckets_as_no_ext() -> None:
    f = _f("Makefile")
    assert source_kind(f) == "other"
    assert bucket_label(f) == "(no ext)"


def test_xlsm_is_treated_as_xlsx() -> None:
    """``.xlsm`` (macro-enabled workbook) goes through the same parser
    as ``.xlsx``; they share the classifier bucket."""
    f = _f("macros.xlsm")
    assert source_kind(f) == "xlsx"


# ---------------------------------------------------------------------------
# Edge: source_url wins over a misleading extension
# ---------------------------------------------------------------------------


def test_source_url_takes_precedence_over_extension() -> None:
    """A Google Doc exported as .md must classify as a Doc, not markdown.
    Same reasoning as the sidebar bucket: the file's *origin* is the
    interesting property, the disk extension is incidental."""
    f = _f("01-Notes.md", "https://docs.google.com/document/d/abc/edit")
    assert source_kind(f) == "google_doc"
    assert bucket_label(f) == "Google Doc"


def test_local_url_does_not_pull_into_workspace_bucket() -> None:
    """A GitHub raw URL doesn't match a Workspace prefix; the
    classifier must fall through to extension."""
    f = _f("script.py", "https://raw.githubusercontent.com/x/y/main/script.py")
    assert source_kind(f) == "other"  # .py isn't in the curated table
    assert bucket_label(f) == ".py"
