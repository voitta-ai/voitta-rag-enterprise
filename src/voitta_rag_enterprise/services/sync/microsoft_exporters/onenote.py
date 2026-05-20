"""OneNote → markdown.

Walks the notebooks attached to a SharePoint site (``/sites/{id}/onenote``)
or a user (``/users/{id}/onenote`` — used by the Teams connector for the
organizer's personal notebooks where "meeting notes" pages often live).

Each page is fetched as HTML and converted to markdown with the
:mod:`markdownify` library if it's available, falling back to a stripped-
HTML approximation otherwise. Pages render into

    <notebook-name>/<section-name>/<page-title>.md

with a frontmatter block carrying notebook / section / page metadata.
The leading ``<!--voitta-fingerprint:lastModifiedDateTime-->`` lets the
next sync skip unchanged pages without re-downloading HTML.
"""

from __future__ import annotations

import html as _html
import logging
import re
from typing import Any

import httpx

from ..microsoft_auth import graph_get, raise_graph_error
from .base import RemoteEntry, fingerprint_header, safe_filename

logger = logging.getLogger(__name__)


async def export_notebooks(
    *,
    client: httpx.AsyncClient,
    token: str,
    owner_url: str,
    base_rel: str = "OneNote",
    deep_link_base: str = "",
) -> list[RemoteEntry]:
    """Enumerate notebooks → sections → pages under ``owner_url``.

    ``owner_url`` is the Graph prefix that owns the notebooks — e.g.
    ``/sites/{site_id}`` or ``/users/{user_id}`` or ``/me``. The base
    URL is ``https://graph.microsoft.com/v1.0`` (added internally).
    ``base_rel`` is the prefix for local paths.

    Returns ``[]`` on 403 (scope not granted) — the caller logs and
    moves on. Per-page failures are caught and skipped individually.
    """
    base = "https://graph.microsoft.com/v1.0"
    notebooks_url: str | None = (
        f"{base}{owner_url}/onenote/notebooks?$select=id,displayName,links"
    )

    entries: list[RemoteEntry] = []
    while notebooks_url:
        resp = await graph_get(client, notebooks_url, token)
        if resp.status_code == 403:
            logger.info(
                "OneNote skipped at %s (403 — Notes.Read.All not granted)",
                owner_url,
            )
            return []
        if resp.status_code != 200:
            raise_graph_error(resp, f"OneNote notebooks at {owner_url}")
        data = resp.json()
        for nb in data.get("value", []):
            entries.extend(
                await _export_notebook(
                    client=client,
                    token=token,
                    notebook=nb,
                    base_rel=base_rel,
                    deep_link_base=deep_link_base,
                )
            )
        notebooks_url = data.get("@odata.nextLink")
    return entries


async def _export_notebook(
    *,
    client: httpx.AsyncClient,
    token: str,
    notebook: dict[str, Any],
    base_rel: str,
    deep_link_base: str,
) -> list[RemoteEntry]:
    nb_name = safe_filename(notebook.get("displayName") or "notebook", "notebook")
    self_link = notebook.get("self") or ""
    if not self_link:
        logger.warning("Notebook %s has no self link — skipping", nb_name)
        return []
    sections_url: str | None = f"{self_link}/sections?$select=id,displayName,self"

    entries: list[RemoteEntry] = []
    while sections_url:
        resp = await graph_get(client, sections_url, token)
        if resp.status_code != 200:
            logger.warning(
                "OneNote sections fetch failed for %s: %s",
                nb_name, resp.status_code,
            )
            break
        data = resp.json()
        for section in data.get("value", []):
            sec_name = safe_filename(
                section.get("displayName") or "section", "section"
            )
            entries.extend(
                await _export_section(
                    client=client,
                    token=token,
                    section=section,
                    rel_dir=f"{base_rel}/{nb_name}/{sec_name}",
                    deep_link_base=deep_link_base,
                    notebook_name=notebook.get("displayName") or "",
                    section_name=section.get("displayName") or "",
                )
            )
        sections_url = data.get("@odata.nextLink")
    return entries


async def _export_section(
    *,
    client: httpx.AsyncClient,
    token: str,
    section: dict[str, Any],
    rel_dir: str,
    deep_link_base: str,
    notebook_name: str,
    section_name: str,
) -> list[RemoteEntry]:
    base = "https://graph.microsoft.com/v1.0"
    self_link = section.get("self") or f"{base}/me/onenote/sections/{section['id']}"
    pages_url: str | None = (
        f"{self_link}/pages?$select=id,title,lastModifiedDateTime,links,contentUrl"
        "&pagelevel=true&$top=100"
    )

    entries: list[RemoteEntry] = []
    while pages_url:
        resp = await graph_get(client, pages_url, token)
        if resp.status_code != 200:
            logger.warning(
                "OneNote pages fetch failed for section %s: %s",
                section_name, resp.status_code,
            )
            break
        data = resp.json()
        for page in data.get("value", []):
            try:
                entry = await _export_page(
                    client=client,
                    token=token,
                    page=page,
                    rel_dir=rel_dir,
                    notebook_name=notebook_name,
                    section_name=section_name,
                )
                if entry is not None:
                    entries.append(entry)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "OneNote page export failed (%s): %s",
                    page.get("title"), e,
                )
        pages_url = data.get("@odata.nextLink")
    return entries


async def _export_page(
    *,
    client: httpx.AsyncClient,
    token: str,
    page: dict[str, Any],
    rel_dir: str,
    notebook_name: str,
    section_name: str,
) -> RemoteEntry | None:
    title = safe_filename(page.get("title") or "untitled", "page")
    fingerprint = page.get("lastModifiedDateTime") or ""
    deep_link = (page.get("links") or {}).get("oneNoteWebUrl", {}).get("href", "")

    content_url = page.get("contentUrl")
    if not content_url:
        return None
    resp = await graph_get(client, content_url, token)
    if resp.status_code != 200:
        logger.warning(
            "OneNote page %s content fetch %s",
            page.get("title"), resp.status_code,
        )
        return None
    html = resp.text or ""
    md = _html_to_markdown(html)

    frontmatter = (
        f"---\n"
        f"notebook: {_yaml_escape(notebook_name)}\n"
        f"section: {_yaml_escape(section_name)}\n"
        f"page: {_yaml_escape(page.get('title') or '')}\n"
        f"last_modified: {fingerprint}\n"
        f"source: onenote\n"
        f"url: {deep_link}\n"
        f"---\n\n"
    )
    payload = fingerprint_header(fingerprint) + frontmatter + md.strip() + "\n"
    return RemoteEntry(
        rel_path=f"{rel_dir}/{title}.md",
        url=deep_link,
        fingerprint=fingerprint,
        payload=payload,
    )


def _yaml_escape(value: str) -> str:
    """Minimal scalar escape — avoid colons + quotes breaking the frontmatter."""
    if value is None:
        return ""
    safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{safe}"' if any(c in safe for c in ':#&*!|>') else safe


def _html_to_markdown(html: str) -> str:
    """Convert OneNote page HTML to markdown.

    Prefers ``markdownify`` (already a transitive dep via several
    extractors in this repo) and falls back to a crude tag-stripper.
    OneNote's HTML is dense — tables, inline images via ``data:`` URIs,
    and lots of inline styles — markdownify handles all of that
    acceptably for indexing purposes.
    """
    try:
        from markdownify import markdownify as md_convert  # type: ignore
    except ImportError:
        return _strip_html(html)
    try:
        return md_convert(html, heading_style="ATX")
    except Exception:  # noqa: BLE001
        return _strip_html(html)


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return _html.unescape(_TAG_RE.sub("", html))
