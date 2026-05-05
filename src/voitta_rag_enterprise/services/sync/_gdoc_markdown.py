"""Google Docs ``documents.get`` body → Markdown renderer.

The Drive ``files.export`` endpoint only ever returns the *primary* tab of a
multi-tab Google Doc. To preserve every tab we call the Docs API with
``includeTabsContent=true`` and walk the structured body ourselves. This
module is the body-walker; the connector handles auth and orchestration.

Output is plain markdown — headings, paragraphs, bullet/ordered lists, and
pipe-tables. Inline images are rendered as ``![alt](drive-url)`` placeholders
when a contentUri is available; image bytes are not downloaded (the existing
parsers extract image bytes from native formats only, and tab markdown is
read by ``TextParser`` which has no image pipeline).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Google Docs `paragraphStyle.namedStyleType` values that map to a heading.
# TITLE / SUBTITLE are flattened to H1/H2 — close enough for retrieval and
# matches what `files.export` does when emitting docx/headings.
_HEADING_LEVEL = {
    "TITLE": 1,
    "SUBTITLE": 2,
    "HEADING_1": 1,
    "HEADING_2": 2,
    "HEADING_3": 3,
    "HEADING_4": 4,
    "HEADING_5": 5,
    "HEADING_6": 6,
}

_SAFE_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def safe_filename(name: str, fallback: str = "tab") -> str:
    """Sanitize a tab title for use as a filesystem path component."""
    out = _SAFE_RE.sub("-", name).strip().strip(".")
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"-{2,}", "-", out)
    return out[:80] or fallback


@dataclass
class RenderedTab:
    """One tab rendered to markdown.

    ``parent_titles`` holds the chain of enclosing tab titles (root to leaf,
    excluding ``title`` itself) — used to build a human-readable display name
    like ``"API > Endpoints"``.
    """

    tab_id: str
    title: str
    parent_titles: list[str]
    markdown: str

    @property
    def display_name(self) -> str:
        return " > ".join([*self.parent_titles, self.title])


def has_tabs(document: dict[str, Any]) -> bool:
    """True if ``document`` exposes the new tabs schema (one or more tabs)."""
    tabs = document.get("tabs") or []
    return bool(tabs)


def render_document_tabs(document: dict[str, Any]) -> list[RenderedTab]:
    """Walk every tab + child tab in ``document``; return rendered markdown.

    Order matches the document's natural tab ordering (depth-first, parents
    before children) so ``enumerate`` indices line up with what the user sees
    in the Docs UI.
    """
    out: list[RenderedTab] = []
    for tab in document.get("tabs") or []:
        _walk_tab(tab, parents=[], out=out)
    return out


def _walk_tab(
    tab: dict[str, Any], parents: list[str], out: list[RenderedTab]
) -> None:
    props = tab.get("tabProperties") or {}
    tab_id = props.get("tabId") or ""
    title = props.get("title") or "Untitled"
    body = (tab.get("documentTab") or {}).get("body") or {}
    md = render_body(body)
    out.append(
        RenderedTab(
            tab_id=tab_id,
            title=title,
            parent_titles=list(parents),
            markdown=md,
        )
    )
    next_parents = [*parents, title]
    for child in tab.get("childTabs") or []:
        _walk_tab(child, next_parents, out)


# ---------------------------------------------------------------------------
# Body walker
# ---------------------------------------------------------------------------


def render_body(body: dict[str, Any]) -> str:
    """Render a Docs API ``body`` (or ``documentTab.body``) as markdown."""
    blocks: list[str] = []
    elements = body.get("content") or []
    i = 0
    while i < len(elements):
        el = elements[i]
        if "paragraph" in el:
            # Coalesce consecutive list items with the same listId so they
            # form a single bullet/ordered block.
            j = i
            list_id = _list_id_of(el)
            if list_id is not None:
                items: list[tuple[int, bool, str]] = []
                while j < len(elements):
                    nxt = elements[j]
                    nxt_list_id = _list_id_of(nxt)
                    if nxt_list_id != list_id:
                        break
                    nesting, ordered, text = _list_item(nxt["paragraph"])
                    items.append((nesting, ordered, text))
                    j += 1
                blocks.append(_format_list(items))
                i = j
                continue
            blocks.append(_render_paragraph(el["paragraph"]))
        elif "table" in el:
            blocks.append(_render_table(el["table"]))
        elif "sectionBreak" in el:
            # Section breaks split logical sections — keep one blank line.
            pass
        elif "tableOfContents" in el:
            # The TOC body just contains paragraphs the user can already see
            # via headings; rendering it would duplicate every heading.
            pass
        i += 1
    return "\n\n".join(b for b in blocks if b)


def _list_id_of(element: dict[str, Any]) -> str | None:
    p = element.get("paragraph")
    if not p:
        return None
    bullet = p.get("bullet")
    if not bullet:
        return None
    return bullet.get("listId")


def _list_item(paragraph: dict[str, Any]) -> tuple[int, bool, str]:
    """Return ``(nesting_level, is_ordered, rendered_text)`` for a list item."""
    bullet = paragraph.get("bullet") or {}
    nesting = int(bullet.get("nestingLevel") or 0)
    text = _runs_to_text(paragraph.get("elements") or []).rstrip("\n")
    # Detect "ordered" by looking at the listId's properties when available;
    # the API exposes glyphSymbol on the list (not the paragraph), so without
    # the ``lists`` map we fall back to a heuristic: glyph "%0" / numeric
    # patterns are ordered. We treat all bullets as unordered by default —
    # that's what google's own docx export does for unrecognised glyphs.
    ordered = False
    return nesting, ordered, text


def _format_list(items: list[tuple[int, bool, str]]) -> str:
    out: list[str] = []
    for nesting, ordered, text in items:
        marker = "1." if ordered else "-"
        indent = "  " * max(0, nesting)
        out.append(f"{indent}{marker} {text}" if text else f"{indent}{marker}")
    return "\n".join(out)


def _render_paragraph(paragraph: dict[str, Any]) -> str:
    style = paragraph.get("paragraphStyle") or {}
    named = style.get("namedStyleType")
    text = _runs_to_text(paragraph.get("elements") or [])
    text = text.rstrip("\n")
    if not text.strip():
        return ""
    level = _HEADING_LEVEL.get(named or "")
    if level:
        return f"{'#' * level} {text.strip()}"
    return text


def _runs_to_text(elements: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for el in elements:
        if "textRun" in el:
            tr = el["textRun"]
            content = tr.get("content") or ""
            ts = tr.get("textStyle") or {}
            link = (ts.get("link") or {}).get("url")
            if link:
                # Trim trailing newline before wrapping in []() so the link
                # syntax doesn't span paragraphs.
                trailing = ""
                if content.endswith("\n"):
                    trailing = "\n"
                    content = content[:-1]
                parts.append(f"[{content}]({link}){trailing}")
            else:
                parts.append(_emphasis(content, ts))
        elif "inlineObjectElement" in el:
            # We don't fetch image bytes (see module docstring). Keep a
            # neutral placeholder so the surrounding prose still chunks
            # well and the reader knows there was an image here.
            parts.append("![image]()")
        elif "person" in el:
            person = el["person"].get("personProperties") or {}
            name = person.get("name") or person.get("email") or "person"
            parts.append(f"@{name}")
        elif "richLink" in el:
            rp = el["richLink"].get("richLinkProperties") or {}
            title = rp.get("title") or "link"
            uri = rp.get("uri") or ""
            parts.append(f"[{title}]({uri})" if uri else title)
        # Other run types (footnoteReference, equation, …) intentionally
        # ignored — they don't carry retrievable text.
    return "".join(parts)


def _emphasis(content: str, ts: dict[str, Any]) -> str:
    """Wrap ``content`` with markdown emphasis markers per ``textStyle``.

    Order matters: bold-italic-code wrapping is applied innermost-first so
    the markers don't end up nested wrong (``**_x_**`` not ``_**x**_``).
    Trailing newlines are kept *outside* the wrappers because markdown
    inline emphasis can't span line breaks.
    """
    if not content or not content.strip():
        return content
    trailing = ""
    while content.endswith("\n"):
        trailing += "\n"
        content = content[:-1]
    if ts.get("italic"):
        content = f"_{content}_"
    if ts.get("bold"):
        content = f"**{content}**"
    return content + trailing


def _render_table(table: dict[str, Any]) -> str:
    rows = table.get("tableRows") or []
    if not rows:
        return ""
    grid: list[list[str]] = []
    for row in rows:
        cells = row.get("tableCells") or []
        grid.append([_render_cell(c) for c in cells])
    width = max((len(r) for r in grid), default=0)
    if width == 0:
        return ""
    for r in grid:
        while len(r) < width:
            r.append("")
    out = ["| " + " | ".join(grid[0]) + " |"]
    out.append("| " + " | ".join(["---"] * width) + " |")
    for r in grid[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _render_cell(cell: dict[str, Any]) -> str:
    """Flatten a table cell to a single line — pipe-tables are line-based."""
    text_parts: list[str] = []
    for el in cell.get("content") or []:
        if "paragraph" in el:
            text_parts.append(_runs_to_text(el["paragraph"].get("elements") or []))
        elif "table" in el:
            # Nested tables can't render as pipe-tables; flatten to "row | row".
            text_parts.append(_flatten_nested_table(el["table"]))
    flat = " ".join(t for t in (s.strip() for s in text_parts) if t)
    return flat.replace("|", "\\|")


def _flatten_nested_table(table: dict[str, Any]) -> str:
    rows = []
    for row in table.get("tableRows") or []:
        cells = []
        for c in row.get("tableCells") or []:
            cells.append(_render_cell(c))
        rows.append(" | ".join(cells))
    return " ; ".join(rows)
