"""Google Docs (``vnd.google-apps.document``) → one markdown file per tab.

Layout produced for a doc named ``Specs`` with tabs ``Intro`` and ``API``::

    Specs/
        01-Intro.md
        02-API.md
        01-Intro/
            images/
                img_1.png
                img_2.png
        02-API/
            images/
                img_1.png

Each markdown file leads with the inline fingerprint comment used by
the connector to skip re-rendering unchanged tabs. Per-image RemoteEntries
fingerprint independently so re-syncs only re-download images that were
added or replaced.

Image bytes are fetched from
``inlineObjects[<id>].embeddedObject.imageProperties.contentUri`` —
ephemeral authenticated URLs Google issues per ``documents.get`` call.
The producer captures the URI at materialize time and downloads with a
bearer-token httpx call (the URLs aren't googleapiclient-shaped); the
URLs typically remain valid long enough to span the listing → download
gap, which is seconds in practice.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ._docs_markdown import (
    ImageReference,
    RenderedTab,
    has_tabs,
    render_document_tabs,
)
from .base import (
    ExportContext,
    NativeDriveExporter,
    ProducerContext,
    RemoteEntry,
    safe_filename,
)

logger = logging.getLogger(__name__)


# Inline fingerprint header. Same shape the connector already uses for
# tab markdown so the existing unchanged-detection path keeps working.
FINGERPRINT_PREFIX = "<!--voitta-fingerprint:"
FINGERPRINT_SUFFIX = "-->"


class DocumentExporter(NativeDriveExporter):
    """Render a Google Doc into one markdown file per tab + per-tab images."""

    mime_type = "application/vnd.google-apps.document"

    def export(
        self,
        item: dict[str, Any],
        rel_no_ext: str,
        ctx: ExportContext,
    ) -> list[RemoteEntry]:
        doc_id = item["id"]
        modified_time = item.get("modifiedTime", "")
        web_url = item.get("webViewLink") or _doc_view_url(doc_id)

        document = (
            ctx.docs()
            .documents()
            .get(documentId=doc_id, includeTabsContent=True)
            .execute()
        )
        if not has_tabs(document):
            # Docs created via the API or before the tabs feature shipped
            # may not surface a tabs structure. Render the body directly
            # under the doc name with a synthetic tab id of "" so the
            # source_url still works and the disk shape is uniform.
            from ._docs_markdown import render_body

            body = document.get("body") or {}
            md, refs = render_body(body, lists=document.get("lists") or {})
            if not md and not refs:
                # Truly empty doc — no markdown to write.
                return []
            return _entries_for_tab(
                tab=RenderedTab(
                    tab_id="",
                    title=safe_filename(item.get("name") or "document"),
                    parent_titles=[],
                    markdown=md,
                    image_references=refs,
                ),
                tab_index=1,
                rel_no_ext=rel_no_ext,
                doc=document,
                web_url=web_url,
                modified_time=modified_time,
                access_token=ctx.access_token,
                only_tab=True,
            )

        tabs = render_document_tabs(document)
        if not tabs:
            return []

        out: list[RemoteEntry] = []
        for index, tab in enumerate(tabs, start=1):
            out.extend(
                _entries_for_tab(
                    tab=tab,
                    tab_index=index,
                    rel_no_ext=rel_no_ext,
                    doc=document,
                    web_url=web_url,
                    modified_time=modified_time,
                    access_token=ctx.access_token,
                    only_tab=False,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Per-tab entry construction
# ---------------------------------------------------------------------------


def _entries_for_tab(
    *,
    tab: RenderedTab,
    tab_index: int,
    rel_no_ext: str,
    doc: dict[str, Any],
    web_url: str,
    modified_time: str,
    access_token: str | None,
    only_tab: bool,
) -> list[RemoteEntry]:
    """Build the markdown + image RemoteEntries for one tab.

    ``only_tab=True`` flattens the on-disk path so a single-tab (or
    no-tabs) doc lands as ``<stem>.md`` with images under ``<stem>/images/``
    rather than nesting under a tab-numbered subdirectory.
    """
    if only_tab:
        md_rel = f"{rel_no_ext}.md"
        img_dir_rel = rel_no_ext  # images/ goes under the doc stem
    else:
        tab_filename = f"{tab_index:02d}-{safe_filename(tab.title)}"
        md_rel = f"{rel_no_ext}/{tab_filename}.md"
        img_dir_rel = f"{rel_no_ext}/{tab_filename}"

    md_url = (
        web_url.split("?")[0] + (f"?tab={tab.tab_id}" if tab.tab_id else "")
    )
    md_fingerprint = f"{modified_time}#{tab.tab_id}"

    # Snapshot the body so the producer's closure doesn't keep the whole
    # document tree alive — just the bytes it'll write.
    body_text = tab.markdown
    out: list[RemoteEntry] = [
        RemoteEntry(
            rel_path=md_rel,
            url=md_url,
            fingerprint=md_fingerprint,
            tab=tab.display_name,
            producer=_make_markdown_producer(body_text, md_fingerprint),
        )
    ]

    inline_objects = doc.get("inlineObjects") or {}
    for ref in tab.image_references:
        spec = inline_objects.get(ref.inline_object_id) or {}
        embedded = spec.get("embeddedObject") or {}
        props = embedded.get("imageProperties") or {}
        content_uri = props.get("contentUri") or ""
        if not content_uri:
            # The Doc references an image but we can't download it (e.g.
            # an unsupported embed type). Skip silently — the markdown
            # link will 404, but the rest of the tab still indexes.
            logger.debug(
                "doc %s: no contentUri for inline object %s; skipping",
                rel_no_ext,
                ref.inline_object_id,
            )
            continue
        out.append(
            RemoteEntry(
                rel_path=f"{img_dir_rel}/{ref.rel_path}",
                url=md_url,  # belongs to the same doc/tab
                fingerprint=f"{modified_time}#{ref.inline_object_id}",
                tab=tab.display_name,
                producer=_make_image_producer(content_uri, access_token),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Producers
# ---------------------------------------------------------------------------


def _make_markdown_producer(
    body: str, fingerprint: str
) -> Callable[[Path, Any, ProducerContext], None]:
    """Pure-text producer: writes the tab markdown atomically with the
    inline fingerprint header. Drive arg + producer ctx are unused."""

    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        _atomic_write_text(
            dest, f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}\n{body}"
        )

    return _produce


def _make_image_producer(
    content_uri: str, access_token: str | None
) -> Callable[[Path, Any, ProducerContext], None]:
    """Producer that downloads image bytes from a Drive inline-object
    contentUri. Uses httpx because contentUri is a raw authenticated URL,
    not a googleapiclient endpoint."""

    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        if not access_token:
            raise RuntimeError(
                "Cannot download Doc inline image without an access token"
            )
        headers = {"Authorization": f"Bearer {access_token}"}
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(content_uri, headers=headers)
            resp.raise_for_status()
        _atomic_write_bytes(dest, resp.content)

    return _produce


# ---------------------------------------------------------------------------
# Atomic-write helpers
# ---------------------------------------------------------------------------
#
# Same pattern as the GoogleDriveConnector's binary downloader: write to a
# unique-suffix tempfile, os.replace onto dest. Lives here (rather than
# imported from google_drive.py) so the exporter package has no upward
# dependency on the connector — connector imports exporter, not the other
# way around.


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


def _doc_view_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"
