"""SharePoint Pages (.aspx) → markdown.

Modern SharePoint Pages are rendered from a structured canvas of
webparts. The Graph endpoint ``/sites/{id}/pages`` returns each page
with a list of ``canvasLayout.horizontalSections[].columns[].webparts``;
each webpart has a ``@odata.type`` discriminator.

We support exporting:

* **textWebPart** (``microsoft.graph.textWebPart``) — already HTML;
  converted to markdown.

Everything else — image webparts, list webparts, custom SPFx parts,
embedded YouTube etc. — gets a one-line placeholder noting the
webpart type. This is intentionally conservative; pages are usually
narrative + the occasional embed, and the narrative is what's
worth indexing.

Pages land at ``Pages/<page-name>.md`` relative to the site root.
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

_TAG_RE = re.compile(r"<[^>]+>")


async def export_site_pages(
    *,
    client: httpx.AsyncClient,
    token: str,
    site_id: str,
    base_rel: str = "Pages",
) -> list[RemoteEntry]:
    """Enumerate ``/sites/{id}/pages`` and export each as markdown.

    Returns ``[]`` on 403 — Pages requires ``Sites.Read.All`` which
    the user might not have granted. Per-page errors are caught.
    """
    base = "https://graph.microsoft.com/v1.0"
    pages_url: str | None = (
        f"{base}/sites/{site_id}/pages/microsoft.graph.sitePage"
        "?$select=id,name,title,webUrl,createdDateTime,lastModifiedDateTime,"
        "createdBy,lastModifiedBy"
    )

    entries: list[RemoteEntry] = []
    while pages_url:
        resp = await graph_get(client, pages_url, token)
        if resp.status_code == 403:
            logger.info(
                "SharePoint Pages skipped for site %s (403 — scope not granted)",
                site_id,
            )
            return []
        if resp.status_code == 404:
            return []  # site has no Pages library
        if resp.status_code != 200:
            raise_graph_error(resp, f"list pages for site {site_id}")
        data = resp.json()
        for page in data.get("value", []):
            try:
                entry = await _export_page(
                    client=client,
                    token=token,
                    site_id=site_id,
                    page=page,
                    base_rel=base_rel,
                )
                if entry is not None:
                    entries.append(entry)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "SharePoint page export failed (%s): %s",
                    page.get("name"), e,
                )
        pages_url = data.get("@odata.nextLink")
    return entries


async def _export_page(
    *,
    client: httpx.AsyncClient,
    token: str,
    site_id: str,
    page: dict[str, Any],
    base_rel: str,
) -> RemoteEntry | None:
    base = "https://graph.microsoft.com/v1.0"
    page_id = page["id"]
    name = page.get("name") or page.get("title") or page_id
    title = page.get("title") or name
    fingerprint = page.get("lastModifiedDateTime") or ""
    deep_link = page.get("webUrl") or ""

    # Fetch the canvas — the list endpoint above doesn't include it.
    canvas_url = (
        f"{base}/sites/{site_id}/pages/{page_id}/microsoft.graph.sitePage"
        "?$expand=canvasLayout"
    )
    resp = await graph_get(client, canvas_url, token)
    if resp.status_code != 200:
        logger.warning(
            "Failed to fetch canvas for page %s: %s", name, resp.status_code
        )
        return None
    body = resp.json()
    md = _canvas_to_markdown(title, body.get("canvasLayout") or {})

    payload = (
        fingerprint_header(fingerprint)
        + f"---\n"
        + f"page: {_yaml_escape(title)}\n"
        + f"last_modified: {fingerprint}\n"
        + f"source: sharepoint_page\n"
        + f"url: {deep_link}\n"
        + f"---\n\n"
        + md.strip()
        + "\n"
    )
    safe = safe_filename(name.removesuffix(".aspx"), "page")
    cb = (page.get("createdBy") or {}).get("user") or {}
    mb = (page.get("lastModifiedBy") or {}).get("user") or {}
    return RemoteEntry(
        rel_path=f"{base_rel}/{safe}.md",
        url=deep_link,
        fingerprint=fingerprint,
        payload=payload,
        # Raw provenance; the connector normalizes it (+ adds shared_by) into
        # source_meta. Same shape onenote/drive-items surface.
        extra={"prov": {
            "created": page.get("createdDateTime"),
            "modified": page.get("lastModifiedDateTime"),
            "created_by_name": cb.get("displayName"),
            "created_by_email": cb.get("email"),
            "modified_by_name": mb.get("displayName"),
            "modified_by_email": mb.get("email"),
        }},
    )


def _canvas_to_markdown(title: str, canvas: dict[str, Any]) -> str:
    parts: list[str] = [f"# {title}\n"]
    for section in canvas.get("horizontalSections") or []:
        for column in section.get("columns") or []:
            for wp in column.get("webparts") or []:
                parts.append(_webpart_to_markdown(wp))
    # Vertical section (sidebar) — same shape, single column.
    vert = canvas.get("verticalSection")
    if vert:
        for wp in vert.get("webparts") or []:
            parts.append(_webpart_to_markdown(wp))
    return "\n\n".join(p for p in parts if p)


def _webpart_to_markdown(webpart: dict[str, Any]) -> str:
    odata_type = webpart.get("@odata.type") or ""
    if odata_type.endswith("textWebPart"):
        inner = webpart.get("innerHtml") or ""
        return _html_to_markdown(inner)
    # Best-effort placeholder for non-text webparts. Naming the kind
    # gives the indexer something to chunk and the human reader a
    # breadcrumb that the page had something else there.
    kind = odata_type.rsplit(".", 1)[-1] or "unknownWebPart"
    title = (
        webpart.get("data", {}).get("title")
        or webpart.get("webPartType")
        or ""
    )
    return f"_[{kind}{': ' + title if title else ''}]_"


def _html_to_markdown(html: str) -> str:
    try:
        from markdownify import markdownify as md_convert  # type: ignore
    except ImportError:
        return _html.unescape(_TAG_RE.sub("", html))
    try:
        return md_convert(html, heading_style="ATX")
    except Exception:  # noqa: BLE001
        return _html.unescape(_TAG_RE.sub("", html))


def _yaml_escape(value: str) -> str:
    if value is None:
        return ""
    safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{safe}"' if any(c in safe for c in ':#&*!|>') else safe
