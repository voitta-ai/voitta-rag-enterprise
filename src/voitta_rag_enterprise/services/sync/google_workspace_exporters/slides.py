"""Google Slides (``vnd.google-apps.presentation``) → one markdown file
per slide, with thumbnail PNG and speaker notes.

Layout produced for ``Pitch.gslides`` with three slides::

    Pitch/
        01-Intro.md
        02-Roadmap.md
        03-Q-and-A.md
        images/
            slide_1.png
            slide_2.png
            slide_3.png

Each markdown leads with the inline fingerprint header so the connector
can short-circuit unchanged slides on the next sync. Speaker notes are
rendered as a blockquote section under the slide body, intentionally
embedded into the same chunk so retrieval against "what does the author
mean by X" lands on slides whose notes (not body) are the actual signal.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .base import (
    ExportContext,
    NativeDriveExporter,
    ProducerContext,
    RemoteEntry,
    execute_with_retry,
    safe_filename,
)

logger = logging.getLogger(__name__)


# Inline fingerprint header — same shape the rest of the package uses
# so the connector's unchanged-detection works uniformly.
FINGERPRINT_PREFIX = "<!--voitta-fingerprint:"
FINGERPRINT_SUFFIX = "-->"

# Thumbnail size. ``LARGE`` is ~1600px on the long edge, plenty for
# embedding-time RAG and a useful resolution if the user clicks
# through to view the chunk's image. ``MEDIUM`` is ~800px (smaller
# cache footprint) but loses fine print on dense slides.
THUMBNAIL_SIZE = "LARGE"


# Google's Slides API caps ``getThumbnail`` at 60 "expensive read"
# requests per minute per user. A single 134-slide deck blows past that
# in under a minute, after which every subsequent thumbnail returns 429
# and the producer fails. We throttle to a value comfortably below the
# cap so a deck-after-deck sync stays inside one user's quota window
# even when the previous deck just exhausted it.
SLIDES_THUMBNAIL_RPM = 50


class _RateLimiter:
    """Process-wide token bucket: at most ``rate`` calls per ``period`` seconds.

    Why a bucket instead of a simple sleep-between-calls: a bucket lets
    bursty traffic happen freely until the cap, then queues the
    over-cap callers so the average rate stays under ``rate / period``.
    Empirically this is what the user perceives as "the first deck
    runs fast, only the long ones slow down".

    Single instance shared across :class:`PresentationExporter`
    invocations because the quota is per-user, not per-deck. ``threading.Lock``
    protects the timestamp deque from concurrent download-pool workers.
    """

    def __init__(self, rate: int, period: float = 60.0) -> None:
        self.rate = rate
        self.period = period
        self._lock = threading.Lock()
        # Sliding window: timestamps (monotonic) of the last ``rate``
        # acquired tokens. Older than ``period`` ago = expired.
        self._stamps: list[float] = []

    def acquire(self) -> None:
        """Block until a token is available, then record the call."""
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.period
                # Drop expired timestamps.
                while self._stamps and self._stamps[0] < cutoff:
                    self._stamps.pop(0)
                if len(self._stamps) < self.rate:
                    self._stamps.append(now)
                    return
                # We're full. Sleep until the oldest stamp expires + a
                # tiny epsilon so the next acquire isn't right on the
                # boundary (Google's window cutoff isn't perfectly
                # synced with ours; a 100ms cushion eliminates the
                # last few stragglers we saw in testing).
                wait = self._stamps[0] + self.period + 0.1 - now
            if wait > 0:
                time.sleep(wait)


# Single shared limiter — Google's per-user quota means concurrent
# decks contend for the same window. ``module-private`` so tests can
# reach in and reset it.
_thumbnail_limiter = _RateLimiter(SLIDES_THUMBNAIL_RPM, period=60.0)


def _reset_thumbnail_limiter_for_tests(rate: int = SLIDES_THUMBNAIL_RPM, period: float = 60.0) -> None:
    """Test hook: rebuild the shared limiter with custom params."""
    global _thumbnail_limiter
    _thumbnail_limiter = _RateLimiter(rate, period)


class PresentationExporter(NativeDriveExporter):
    """Render a Google Slides deck into one markdown file per slide,
    each carrying the slide's body text, a thumbnail image, and any
    speaker notes."""

    mime_type = "application/vnd.google-apps.presentation"

    def export(
        self,
        item: dict[str, Any],
        rel_no_ext: str,
        ctx: ExportContext,
    ) -> list[RemoteEntry]:
        deck_id = item["id"]
        modified_time = item.get("modifiedTime", "")
        web_url = item.get("webViewLink") or _deck_view_url(deck_id)

        # Pull the structural payload once; the producers below run on
        # the captured snapshot.
        deck = execute_with_retry(
            ctx.slides()
            .presentations()
            .get(presentationId=deck_id),
            label="slides",
        )
        slides = deck.get("slides") or []
        if not slides:
            return []

        out: list[RemoteEntry] = []
        for index, slide in enumerate(slides, start=1):
            slide_id = slide.get("objectId") or f"slide_{index}"
            title, body, notes = _extract_slide_text(slide)
            display_title = title.strip() or f"Slide {index}"
            safe_title = safe_filename(display_title, fallback=f"slide-{index}")
            md_rel = f"{rel_no_ext}/{index:02d}-{safe_title}.md"
            md_url = f"{web_url.split('?')[0].split('#')[0]}#slide=id.{slide_id}"
            md_fp = f"{modified_time}#slide:{slide_id}"

            md_body = _render_slide_markdown(
                index=index,
                display_title=display_title,
                body=body,
                notes=notes,
            )
            out.append(
                RemoteEntry(
                    rel_path=md_rel,
                    url=md_url,
                    fingerprint=md_fp,
                    tab=display_title,
                    producer=_make_markdown_producer(md_body, md_fp),
                )
            )

            # Per-slide thumbnail. Lives next to all the other slide
            # images under ``<stem>/images/`` so reading any one slide's
            # markdown resolves a relative path on disk.
            out.append(
                RemoteEntry(
                    rel_path=f"{rel_no_ext}/images/slide_{index}.png",
                    url=md_url,
                    fingerprint=f"{modified_time}#slide_thumb:{slide_id}",
                    tab=display_title,
                    producer=_make_thumbnail_producer(deck_id, slide_id),
                )
            )
        return out


# ---------------------------------------------------------------------------
# Slide content extraction
# ---------------------------------------------------------------------------


def _extract_slide_text(slide: dict[str, Any]) -> tuple[str, str, str]:
    """Pull (title, body, notes) text out of one slide.

    The Slides API distinguishes title placeholders by
    ``placeholder.type`` ∈ {``TITLE``, ``CENTERED_TITLE``,
    ``SUBTITLE``}. Anything else with text content is body. Speaker
    notes live under ``slideProperties.notesPage`` with the same
    pageElement structure.
    """
    title_parts: list[str] = []
    body_parts: list[str] = []
    for el in slide.get("pageElements") or []:
        text = _text_from_page_element(el)
        if not text:
            continue
        ph_type = (
            ((el.get("shape") or {}).get("placeholder") or {}).get("type")
        )
        if ph_type in ("TITLE", "CENTERED_TITLE", "SUBTITLE"):
            title_parts.append(text)
        else:
            body_parts.append(text)
    notes_parts: list[str] = []
    notes_page = (slide.get("slideProperties") or {}).get("notesPage") or {}
    for el in notes_page.get("pageElements") or []:
        text = _text_from_page_element(el)
        if text:
            notes_parts.append(text)
    return (
        "\n".join(t for t in title_parts if t.strip()),
        "\n\n".join(t for t in body_parts if t.strip()),
        "\n\n".join(t for t in notes_parts if t.strip()),
    )


def _text_from_page_element(element: dict[str, Any]) -> str:
    """Flatten one pageElement's nested text runs to a string.

    Handles plain shapes, group recursion (groups nest pageElements),
    and table cells (rows of cells, each carrying its own text).
    Non-text elements (images, video, lines) return empty.
    """
    if "elementGroup" in element:
        children = (element["elementGroup"] or {}).get("children") or []
        return "\n".join(t for t in (_text_from_page_element(c) for c in children) if t)

    if "table" in element:
        cells: list[str] = []
        for row in (element["table"] or {}).get("tableRows") or []:
            for cell in row.get("tableCells") or []:
                txt = _runs_to_text((cell.get("text") or {}).get("textElements") or [])
                if txt.strip():
                    cells.append(txt)
        return "\n".join(cells)

    shape = element.get("shape") or {}
    text = (shape.get("text") or {}).get("textElements") or []
    return _runs_to_text(text)


def _runs_to_text(elements: list[dict[str, Any]]) -> str:
    """Flatten ``textElements[]`` to a string. We don't carry over rich
    formatting here — slide bodies are short and the embedder cares about
    the words, not the styling. ``\\v`` (vertical tabs Google uses for
    soft line breaks) collapse to newlines so paragraph splits survive.
    """
    parts: list[str] = []
    for el in elements:
        tr = el.get("textRun")
        if not tr:
            continue
        content = tr.get("content") or ""
        # Slides emit ``\v`` (vertical tab) for shift-Enter line
        # breaks; normalise to newline so the paragraph reads correctly.
        content = content.replace("\v", "\n")
        parts.append(content)
    return "".join(parts).rstrip()


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_slide_markdown(
    *, index: int, display_title: str, body: str, notes: str
) -> str:
    """Compose one slide's markdown.

    The image reference is intentionally below the heading and above the
    body text so a chunk that lands on the slide carries (heading +
    figure + body + notes) in document order.
    """
    parts: list[str] = [f"# Slide {index}: {display_title}", ""]
    parts.append(f"![](images/slide_{index}.png)")
    if body.strip():
        parts.append("")
        parts.append(body.strip())
    if notes.strip():
        parts.append("")
        parts.append("> Speaker notes:")
        for line in notes.splitlines():
            parts.append(f"> {line}" if line.strip() else ">")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Producers
# ---------------------------------------------------------------------------


def _make_markdown_producer(
    body: str, fingerprint: str
) -> Callable[[Path, Any, ProducerContext], None]:
    """Pure-text producer; writes the slide markdown atomically."""

    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        text = f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}\n{body}"
        _atomic_write_text(dest, text)

    return _produce


def _make_thumbnail_producer(
    deck_id: str, slide_id: str
) -> Callable[[Path, Any, ProducerContext], None]:
    """Producer that fetches a slide thumbnail.

    Two-step: ``presentations.pages.getThumbnail`` returns a
    ``contentUrl`` we then GET via httpx. The contentUrl is a Google-
    issued temporary public URL that doesn't need a bearer header
    (Google uses signed URL parameters), but we go through httpx
    rather than urllib for retry / timeout uniformity with the rest
    of the connector.
    """

    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        # Block until the per-user quota window has room. Google's
        # 'expensive read' counter ticks before we even get a 429
        # response, so the bucket has to gate the API call (not the
        # httpx GET on contentUrl, which is free).
        _thumbnail_limiter.acquire()
        slides = ctx.slides()
        resp = execute_with_retry(
            slides.presentations()
            .pages()
            .getThumbnail(
                presentationId=deck_id,
                pageObjectId=slide_id,
                thumbnailProperties_thumbnailSize=THUMBNAIL_SIZE,
            ),
            label="slides",
        )
        content_url = resp.get("contentUrl")
        if not content_url:
            raise RuntimeError(
                f"Slides API returned no contentUrl for slide {slide_id}"
            )
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(content_url)
            r.raise_for_status()
        _atomic_write_bytes(dest, r.content)

    return _produce


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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


def _deck_view_url(deck_id: str) -> str:
    return f"https://docs.google.com/presentation/d/{deck_id}/edit"
