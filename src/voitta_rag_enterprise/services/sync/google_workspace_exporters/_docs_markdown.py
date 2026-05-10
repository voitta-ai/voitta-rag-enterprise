"""Google Docs ``documents.get`` body → Markdown renderer.

The Drive ``files.export`` endpoint only ever returns the *primary* tab of a
multi-tab Google Doc. To preserve every tab we call the Docs API with
``includeTabsContent=true`` and walk the structured body ourselves. This
module is the body-walker; :class:`docs.DocumentExporter` handles auth,
orchestration, and image bytes download.

Output is plain markdown — headings, paragraphs, bullet/ordered lists,
pipe-tables, inline code / bold / italic / strikethrough. Inline images
are rendered as ``![alt](images/img_<n>.png)`` references; the exporter
downloads the bytes from the corresponding ``inlineObjects[*].embeddedObject.imageProperties.contentUri``
and writes them at the referenced path. The renderer surfaces the
mapping via :class:`RenderedTab.image_references` so the exporter knows
which inline-object IDs must be fetched.

Out of scope: footnotes, equations, suggestions, comments — these don't
carry retrievable text in a useful way and would muddy the embeddings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Google Docs ``paragraphStyle.namedStyleType`` values that map to a heading.
# TITLE / SUBTITLE flatten to H1/H2 — close enough for retrieval and matches
# what ``files.export`` does when emitting docx.
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

# Bullet glyph patterns the Docs API uses for "ordered" lists in
# ``lists[listId].listProperties.nestingLevels[N].glyphType``. We treat any
# of these as ordered; the rest as unordered. ``glyphType`` is the modern
# field; older API responses use ``glyphSymbol`` which we fall back to.
_ORDERED_GLYPH_TYPES = {
    "DECIMAL",
    "ZERO_DECIMAL",
    "UPPER_ALPHA",
    "ALPHA",
    "UPPER_ROMAN",
    "ROMAN",
}


@dataclass
class ImageReference:
    """One inline image the renderer placed in the markdown output.

    The exporter looks up ``inline_object_id`` in
    ``document.inlineObjects[<id>].embeddedObject.imageProperties.contentUri``
    to get the bytes-source URL, then writes the file at ``rel_path``
    so the markdown reference resolves.
    """

    inline_object_id: str
    rel_path: str  # e.g. "images/img_1.png"
    alt: str = ""


@dataclass
class RenderedTab:
    """One tab rendered to markdown.

    ``parent_titles`` holds the chain of enclosing tab titles (root to
    leaf, excluding ``title`` itself) — used to build a human-readable
    display name like ``"API > Endpoints"``.

    ``image_references`` lists every inline image the body wants — the
    exporter downloads them after rendering.
    """

    tab_id: str
    title: str
    parent_titles: list[str]
    markdown: str
    image_references: list[ImageReference] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return " > ".join([*self.parent_titles, self.title])


def has_tabs(document: dict[str, Any]) -> bool:
    """True if ``document`` exposes the new tabs schema (one or more tabs)."""
    return bool(document.get("tabs"))


def render_document_tabs(document: dict[str, Any]) -> list[RenderedTab]:
    """Walk every tab + child tab in ``document``; return rendered markdown.

    Order matches the document's natural tab ordering (depth-first,
    parents before children) so ``enumerate`` indices line up with what
    the user sees in the Docs UI.

    The ``lists`` map (top-level on ``document``) is threaded through so
    the renderer can look up bullet glyph type for ordered/unordered
    detection.
    """
    out: list[RenderedTab] = []
    lists = document.get("lists") or {}
    for tab in document.get("tabs") or []:
        _walk_tab(tab, parents=[], lists=lists, out=out)
    return out


def _walk_tab(
    tab: dict[str, Any],
    parents: list[str],
    lists: dict[str, Any],
    out: list[RenderedTab],
) -> None:
    props = tab.get("tabProperties") or {}
    tab_id = props.get("tabId") or ""
    title = props.get("title") or "Untitled"
    body = (tab.get("documentTab") or {}).get("body") or {}
    md, refs = _render_body(body, lists=lists)
    out.append(
        RenderedTab(
            tab_id=tab_id,
            title=title,
            parent_titles=list(parents),
            markdown=md,
            image_references=refs,
        )
    )
    next_parents = [*parents, title]
    for child in tab.get("childTabs") or []:
        _walk_tab(child, next_parents, lists, out)


# ---------------------------------------------------------------------------
# Body walker
# ---------------------------------------------------------------------------


class _RenderState:
    """Mutable state threaded through the body walker.

    Lets us assign sequential ids to inline images and track ordered-list
    counters per nesting level without passing growing tuples through
    every helper.
    """

    def __init__(self, lists: dict[str, Any]) -> None:
        self.lists = lists
        self.image_counter = 0
        self.image_references: list[ImageReference] = []


def _render_body(
    body: dict[str, Any], lists: dict[str, Any]
) -> tuple[str, list[ImageReference]]:
    state = _RenderState(lists)
    blocks: list[str] = []
    elements = body.get("content") or []
    i = 0
    while i < len(elements):
        el = elements[i]
        if "paragraph" in el:
            list_id = _list_id_of(el)
            if list_id is not None:
                # Coalesce consecutive list items with the same listId
                # so they form a single bullet/ordered block.
                j = i
                items: list[tuple[int, bool, str]] = []
                while j < len(elements):
                    nxt = elements[j]
                    if _list_id_of(nxt) != list_id:
                        break
                    items.append(_list_item(nxt["paragraph"], list_id, state))
                    j += 1
                blocks.append(_format_list(items))
                i = j
                continue
            blocks.append(_render_paragraph(el["paragraph"], state))
        elif "table" in el:
            blocks.append(_render_table(el["table"], state))
        elif "sectionBreak" in el:
            # Section breaks split logical sections — keep one blank line.
            pass
        elif "tableOfContents" in el:
            # The TOC body just contains paragraphs the user can already
            # see via headings; rendering it would duplicate every heading.
            pass
        i += 1
    return "\n\n".join(b for b in blocks if b), state.image_references


def _list_id_of(element: dict[str, Any]) -> str | None:
    p = element.get("paragraph")
    if not p:
        return None
    bullet = p.get("bullet")
    if not bullet:
        return None
    return bullet.get("listId")


def _is_ordered_list(state: _RenderState, list_id: str, nesting: int) -> bool:
    """Look up the bullet glyph for ``(list_id, nesting)`` and decide
    whether we render this list as ordered."""
    spec = state.lists.get(list_id) or {}
    levels = (spec.get("listProperties") or {}).get("nestingLevels") or []
    if 0 <= nesting < len(levels):
        level = levels[nesting] or {}
        glyph_type = level.get("glyphType")
        if glyph_type:
            return glyph_type in _ORDERED_GLYPH_TYPES
    return False


def _list_item(
    paragraph: dict[str, Any], list_id: str, state: _RenderState
) -> tuple[int, bool, str]:
    """Return ``(nesting_level, is_ordered, rendered_text)`` for a list item."""
    bullet = paragraph.get("bullet") or {}
    nesting = int(bullet.get("nestingLevel") or 0)
    text = _runs_to_text(paragraph.get("elements") or [], state).rstrip("\n")
    ordered = _is_ordered_list(state, list_id, nesting)
    return nesting, ordered, text


def _format_list(items: list[tuple[int, bool, str]]) -> str:
    """Render a coalesced list as nested markdown.

    Ordered counters are tracked per-nesting-level and reset whenever
    we descend or ascend so ``1, 1.1, 1.2, 2`` lays out correctly even
    when ordered + unordered bullets are mixed inside one ``listId``.
    """
    out: list[str] = []
    counters: dict[int, int] = {}
    last_nesting = -1
    for nesting, ordered, text in items:
        # Reset deeper counters when we ascend.
        for level in list(counters):
            if level > nesting:
                counters.pop(level, None)
        if ordered:
            counters[nesting] = counters.get(nesting, 0) + 1
            marker = f"{counters[nesting]}."
        else:
            counters.pop(nesting, None)
            marker = "-"
        indent = "  " * max(0, nesting)
        out.append(f"{indent}{marker} {text}" if text else f"{indent}{marker}")
        last_nesting = nesting
    _ = last_nesting  # silence linters; tracking would help future debugging
    return "\n".join(out)


def _render_paragraph(paragraph: dict[str, Any], state: _RenderState) -> str:
    style = paragraph.get("paragraphStyle") or {}
    named = style.get("namedStyleType")
    text = _runs_to_text(paragraph.get("elements") or [], state)
    text = text.rstrip("\n")
    if not text.strip():
        return ""
    level = _HEADING_LEVEL.get(named or "")
    if level:
        return f"{'#' * level} {text.strip()}"
    return text


def _runs_to_text(elements: list[dict[str, Any]], state: _RenderState) -> str:
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
            obj_id = el["inlineObjectElement"].get("inlineObjectId") or ""
            if not obj_id:
                continue
            state.image_counter += 1
            rel = f"images/img_{state.image_counter}.png"
            state.image_references.append(
                ImageReference(inline_object_id=obj_id, rel_path=rel, alt="")
            )
            parts.append(f"![]({rel})")
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

    Order matters: applied innermost-first so wrappers don't end up
    nested wrong (``**_x_**`` not ``_**x**_``). Trailing newlines are
    kept *outside* the wrappers because markdown inline emphasis can't
    span line breaks.

    Recognised flags: ``italic``, ``bold``, ``strikethrough``, plus the
    Docs API's ``weightedFontFamily.fontFamily`` set to a monospace
    family (a common convention for inline code in Docs since the API
    has no first-class "code" run flag).
    """
    if not content or not content.strip():
        return content
    trailing = ""
    while content.endswith("\n"):
        trailing += "\n"
        content = content[:-1]
    if _is_monospace(ts):
        content = f"`{content}`"
    if ts.get("italic"):
        content = f"_{content}_"
    if ts.get("bold"):
        content = f"**{content}**"
    if ts.get("strikethrough"):
        content = f"~~{content}~~"
    return content + trailing


# Font families that Docs users commonly pick to mean "this is code".
# Names are case-sensitive in the API.
_MONOSPACE_FONTS = frozenset(
    {
        "Courier New",
        "Roboto Mono",
        "Source Code Pro",
        "Inconsolata",
        "Consolas",
        "Menlo",
        "Monaco",
        "Fira Code",
        "JetBrains Mono",
    }
)


def _is_monospace(ts: dict[str, Any]) -> bool:
    fam = (ts.get("weightedFontFamily") or {}).get("fontFamily") or ""
    return fam in _MONOSPACE_FONTS


def _render_table(table: dict[str, Any], state: _RenderState) -> str:
    rows = table.get("tableRows") or []
    if not rows:
        return ""
    grid: list[list[str]] = []
    for row in rows:
        cells = row.get("tableCells") or []
        grid.append([_render_cell(c, state) for c in cells])
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


def _render_cell(cell: dict[str, Any], state: _RenderState) -> str:
    """Flatten a table cell to a single line — pipe-tables are line-based."""
    text_parts: list[str] = []
    for el in cell.get("content") or []:
        if "paragraph" in el:
            text_parts.append(_runs_to_text(el["paragraph"].get("elements") or [], state))
        elif "table" in el:
            # Nested tables can't render as pipe-tables; flatten to "row | row".
            text_parts.append(_flatten_nested_table(el["table"], state))
    flat = " ".join(t for t in (s.strip() for s in text_parts) if t)
    return flat.replace("|", "\\|")


def _flatten_nested_table(table: dict[str, Any], state: _RenderState) -> str:
    rows = []
    for row in table.get("tableRows") or []:
        cells = []
        for c in row.get("tableCells") or []:
            cells.append(_render_cell(c, state))
        rows.append(" | ".join(cells))
    return " ; ".join(rows)
