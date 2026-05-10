"""Google Forms (``vnd.google-apps.form``) → text-only markdown.

Forms have a structural API that surfaces the title, description, and
every section / question with its options. Rendering this directly as
markdown is far cleaner than the PDF export Drive offers (the PDF is
just the question list with no cleanup), and it keeps the form's
content embeddable as plain text.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import (
    ExportContext,
    NativeDriveExporter,
    ProducerContext,
    RemoteEntry,
)


# Inline fingerprint header — same shape the rest of the package uses.
FINGERPRINT_PREFIX = "<!--voitta-fingerprint:"
FINGERPRINT_SUFFIX = "-->"


class FormExporter(NativeDriveExporter):
    """Render a Google Form into a single markdown file."""

    mime_type = "application/vnd.google-apps.form"

    def export(
        self,
        item: dict[str, Any],
        rel_no_ext: str,
        ctx: ExportContext,  # noqa: ARG002
    ) -> list[RemoteEntry]:
        form_id = item["id"]
        modified_time = item.get("modifiedTime", "")
        web_url = (
            item.get("webViewLink")
            or f"https://docs.google.com/forms/d/{form_id}/edit"
        )
        # Forms is small enough that rendering at producer time is fine
        # and saves a Forms API call when the form hasn't changed.
        return [
            RemoteEntry(
                rel_path=f"{rel_no_ext}.md",
                url=web_url,
                fingerprint=modified_time,
                tab=None,
                producer=_make_form_producer(form_id, modified_time),
            )
        ]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_form_markdown(form: dict[str, Any]) -> str:
    """Render the structural Forms API payload as markdown.

    Layout: title (h1) → description (paragraph) → for each item, a
    section break (h2) or question (h3) plus the question type tag and
    option list. Required questions are flagged inline.
    """
    info = form.get("info") or {}
    title = info.get("title") or "Untitled form"
    description = (info.get("description") or "").strip()

    lines: list[str] = [f"# {title}"]
    if description:
        lines.append("")
        lines.append(description)

    for item in form.get("items") or []:
        lines.append("")
        rendered = _render_item(item)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def _render_item(item: dict[str, Any]) -> str:
    """Render one form item.

    The Forms API uses a tagged union: an item is either a
    ``pageBreakItem`` (section header), a ``questionItem`` (single
    question), a ``questionGroupItem`` (grid of questions sharing
    options), an ``imageItem``, a ``videoItem``, or a ``textItem``.
    """
    title = (item.get("title") or "").strip()
    description = (item.get("description") or "").strip()

    if "pageBreakItem" in item:
        out = [f"## {title or 'Section'}"]
        if description:
            out.append("")
            out.append(description)
        return "\n".join(out)

    if "textItem" in item:
        # Free-form text the form author placed between questions.
        out = []
        if title:
            out.append(f"### {title}")
        if description:
            if out:
                out.append("")
            out.append(description)
        return "\n".join(out)

    if "imageItem" in item or "videoItem" in item:
        # Media items don't have a useful text representation; surface
        # the title only so search can land on the section near them.
        return f"### {title}" if title else ""

    if "questionItem" in item:
        return _render_question(
            title=title,
            description=description,
            question=(item["questionItem"] or {}).get("question") or {},
        )

    if "questionGroupItem" in item:
        # A grid of questions sharing options (e.g. "rate each on
        # 1..5"). Render the group title + each row as its own H4.
        group = item["questionGroupItem"] or {}
        group_questions = group.get("questions") or []
        out = []
        if title:
            out.append(f"### {title}")
        if description:
            out.append("")
            out.append(description)
        for q in group_questions:
            row_title = (q.get("rowQuestion") or {}).get("title") or ""
            if row_title:
                out.append("")
                out.append(f"#### {row_title}")
        # The shared option list lives on the group's grid.
        grid = group.get("grid") or {}
        options = ((grid.get("columns") or {}).get("options")) or []
        if options:
            out.append("")
            for opt in options:
                lbl = opt.get("value") or ""
                if lbl:
                    out.append(f"- {lbl}")
        return "\n".join(out)

    return ""


def _render_question(
    *, title: str, description: str, question: dict[str, Any]
) -> str:
    """Render one questionItem's question."""
    required = bool(question.get("required"))
    qtype = _question_type_label(question)
    header_bits = [f"### {title or 'Question'}"]
    if required:
        header_bits.append("*[required]*")
    header_bits.append(f"_({qtype})_")
    out = [" ".join(header_bits)]
    if description:
        out.append("")
        out.append(description)

    # Options for choice / scale-shaped questions.
    cq = question.get("choiceQuestion") or {}
    if cq:
        for opt in cq.get("options") or []:
            label = opt.get("value") or ""
            if not label and opt.get("isOther"):
                label = "Other…"
            if label:
                out.append(f"- {label}")
    elif question.get("scaleQuestion"):
        scale = question["scaleQuestion"] or {}
        low = scale.get("low")
        high = scale.get("high")
        low_label = scale.get("lowLabel") or ""
        high_label = scale.get("highLabel") or ""
        out.append(
            f"- Scale: {low}{f' ({low_label})' if low_label else ''} → "
            f"{high}{f' ({high_label})' if high_label else ''}"
        )
    return "\n".join(out)


def _question_type_label(question: dict[str, Any]) -> str:
    """Human-readable label for the question's discriminator union."""
    if (cq := question.get("choiceQuestion")):
        # ``type`` ∈ RADIO, CHECKBOX, DROP_DOWN
        ctype = (cq.get("type") or "").lower().replace("_", " ")
        return f"{ctype} choice" if ctype else "choice"
    if "textQuestion" in question:
        tq = question.get("textQuestion") or {}
        return "long-answer text" if tq.get("paragraph") else "short-answer text"
    if "scaleQuestion" in question:
        return "scale"
    if "dateQuestion" in question:
        return "date"
    if "timeQuestion" in question:
        return "time"
    if "fileUploadQuestion" in question:
        return "file upload"
    if "rowQuestion" in question:
        return "grid row"
    if "ratingQuestion" in question:
        return "rating"
    return "question"


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


def _make_form_producer(
    form_id: str, fingerprint: str
) -> Callable[[Path, Any, ProducerContext], None]:
    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        forms = ctx.forms()
        form = forms.forms().get(formId=form_id).execute()
        body = render_form_markdown(form)
        text = f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}\n{body}"
        _atomic_write_text(dest, text)

    return _produce


def _atomic_write_text(dest: Path, text: str) -> None:
    tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
