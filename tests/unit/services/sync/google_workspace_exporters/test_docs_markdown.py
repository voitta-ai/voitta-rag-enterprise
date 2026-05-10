"""Tests for the Google Docs ``documents.get`` body → markdown renderer.

Fixtures here mirror real Docs API JSON shapes. We don't go to the network;
the renderer is pure-python over plain dicts.
"""

from __future__ import annotations

from voitta_rag_enterprise.services.sync.google_workspace_exporters._docs_markdown import (
    has_tabs,
    render_document_tabs,
)
from voitta_rag_enterprise.services.sync.google_workspace_exporters._docs_markdown import (
    _render_body as render_body,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _para(text: str, *, named: str | None = None, bold: bool = False) -> dict:
    style: dict = {}
    if bold:
        style["bold"] = True
    elem = {"textRun": {"content": text, "textStyle": style}}
    para: dict = {"elements": [elem]}
    if named:
        para["paragraphStyle"] = {"namedStyleType": named}
    return {"paragraph": para}


def _bullet(text: str, *, list_id: str = "L1", nesting: int = 0) -> dict:
    return {
        "paragraph": {
            "elements": [{"textRun": {"content": text, "textStyle": {}}}],
            "bullet": {"listId": list_id, "nestingLevel": nesting},
        }
    }


def _ordered_list_spec(list_id: str = "L1") -> dict:
    """A ``lists`` map entry that flags every nesting level as DECIMAL,
    triggering ordered-list rendering."""
    return {
        list_id: {
            "listProperties": {
                "nestingLevels": [
                    {"glyphType": "DECIMAL"},
                    {"glyphType": "DECIMAL"},
                    {"glyphType": "DECIMAL"},
                ]
            }
        }
    }


def _md(body: dict, lists: dict | None = None) -> str:
    return render_body(body, lists=lists or {})[0]


# ---------------------------------------------------------------------------
# Headings + paragraphs
# ---------------------------------------------------------------------------


def test_render_body_emits_headings_and_paragraphs() -> None:
    body = {
        "content": [
            _para("Title here", named="HEADING_1"),
            _para("Body paragraph one."),
            _para("Body paragraph two."),
        ]
    }
    md = _md(body)
    assert md.startswith("# Title here")
    assert "Body paragraph one." in md
    assert "Body paragraph two." in md
    # Headings should be on their own block, separated by a blank line.
    assert md.split("\n\n")[0] == "# Title here"


def test_render_body_h1_through_h6() -> None:
    body = {
        "content": [
            _para("a", named="HEADING_1"),
            _para("b", named="HEADING_2"),
            _para("c", named="HEADING_3"),
            _para("d", named="HEADING_4"),
            _para("e", named="HEADING_5"),
            _para("f", named="HEADING_6"),
        ]
    }
    md = _md(body)
    expected = "# a\n\n## b\n\n### c\n\n#### d\n\n##### e\n\n###### f"
    assert md == expected


def test_title_and_subtitle_flatten_to_h1_h2() -> None:
    body = {
        "content": [
            _para("Big", named="TITLE"),
            _para("Sub", named="SUBTITLE"),
        ]
    }
    assert _md(body) == "# Big\n\n## Sub"


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def test_render_body_groups_consecutive_bullets() -> None:
    body = {
        "content": [
            _bullet("First"),
            _bullet("Second"),
            _bullet("Nested", nesting=1),
            _para("After list."),
        ]
    }
    md = _md(body)
    assert "- First" in md
    assert "- Second" in md
    assert "  - Nested" in md
    # The list block lives in a single paragraph; a separate "After list."
    # block follows it.
    assert "After list." in md.split("\n\n")[-1]


def test_unordered_list_when_no_lists_metadata() -> None:
    """Without ``lists[listId]`` metadata, we conservatively render as
    unordered — same as the Drive docx export does for unrecognised glyphs."""
    body = {
        "content": [
            _bullet("a"),
            _bullet("b"),
        ]
    }
    md = _md(body, lists={})
    assert "- a" in md and "- b" in md
    assert "1." not in md


def test_ordered_list_renders_with_numeric_markers() -> None:
    """``glyphType=DECIMAL`` in the lists map flips us to ordered."""
    body = {
        "content": [
            _bullet("first"),
            _bullet("second"),
            _bullet("third"),
        ]
    }
    md = _md(body, lists=_ordered_list_spec())
    assert "1. first" in md
    assert "2. second" in md
    assert "3. third" in md


def test_ordered_list_resets_counter_after_unindent() -> None:
    """Counters at deeper levels reset when the level closes."""
    body = {
        "content": [
            _bullet("A"),
            _bullet("A1", nesting=1),
            _bullet("A2", nesting=1),
            _bullet("B"),
            _bullet("B1", nesting=1),
        ]
    }
    md = _md(body, lists=_ordered_list_spec())
    # Outer counter advances; nested counter restarts when we descend again.
    assert "1. A" in md
    assert "  1. A1" in md
    assert "  2. A2" in md
    assert "2. B" in md
    assert "  1. B1" in md


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_render_body_renders_table_as_pipe_table() -> None:
    body = {
        "content": [
            {
                "table": {
                    "tableRows": [
                        {
                            "tableCells": [
                                {"content": [_para("a")]},
                                {"content": [_para("b")]},
                            ]
                        },
                        {
                            "tableCells": [
                                {"content": [_para("c")]},
                                {"content": [_para("d|with-pipe")]},
                            ]
                        },
                    ]
                }
            }
        ]
    }
    md = _md(body)
    lines = md.splitlines()
    assert lines[0] == "| a | b |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| c | d\\|with-pipe |"


# ---------------------------------------------------------------------------
# Emphasis (bold/italic/strikethrough/code/link)
# ---------------------------------------------------------------------------


def test_emphasis_bold_italic() -> None:
    elem_bold = {"textRun": {"content": "loud", "textStyle": {"bold": True}}}
    elem_plain = {"textRun": {"content": " quiet", "textStyle": {}}}
    body = {"content": [{"paragraph": {"elements": [elem_bold, elem_plain]}}]}
    assert _md(body) == "**loud** quiet"


def test_emphasis_strikethrough() -> None:
    elem = {"textRun": {"content": "old", "textStyle": {"strikethrough": True}}}
    body = {"content": [{"paragraph": {"elements": [elem]}}]}
    assert _md(body) == "~~old~~"


def test_emphasis_code_via_monospace_font() -> None:
    elem = {
        "textRun": {
            "content": "f(x)",
            "textStyle": {"weightedFontFamily": {"fontFamily": "Courier New"}},
        }
    }
    body = {"content": [{"paragraph": {"elements": [elem]}}]}
    assert _md(body) == "`f(x)`"


def test_emphasis_combined_bold_italic_strike() -> None:
    """Order: code → italic → bold → strike (innermost first)."""
    elem = {
        "textRun": {
            "content": "x",
            "textStyle": {"bold": True, "italic": True, "strikethrough": True},
        }
    }
    body = {"content": [{"paragraph": {"elements": [elem]}}]}
    # _x_ → **_x_** → ~~**_x_**~~
    assert _md(body) == "~~**_x_**~~"


def test_link_textstyle() -> None:
    elem = {
        "textRun": {
            "content": "voitta",
            "textStyle": {"link": {"url": "https://example.invalid/v"}},
        }
    }
    body = {"content": [{"paragraph": {"elements": [elem]}}]}
    assert _md(body) == "[voitta](https://example.invalid/v)"


# ---------------------------------------------------------------------------
# Inline images → references
# ---------------------------------------------------------------------------


def test_inline_image_emits_markdown_reference_and_records_id() -> None:
    body = {
        "content": [
            {
                "paragraph": {
                    "elements": [
                        {"textRun": {"content": "before ", "textStyle": {}}},
                        {"inlineObjectElement": {"inlineObjectId": "kix.abc123"}},
                        {"textRun": {"content": " after", "textStyle": {}}},
                    ]
                }
            }
        ]
    }
    md, refs = render_body(body, lists={})
    assert "before ![](images/img_1.png) after" in md
    assert len(refs) == 1
    assert refs[0].inline_object_id == "kix.abc123"
    assert refs[0].rel_path == "images/img_1.png"


def test_multiple_inline_images_get_unique_paths() -> None:
    elem_image = lambda i: {"inlineObjectElement": {"inlineObjectId": f"kix.{i}"}}
    body = {
        "content": [
            {
                "paragraph": {
                    "elements": [
                        elem_image(1),
                        {"textRun": {"content": " ", "textStyle": {}}},
                        elem_image(2),
                    ]
                }
            }
        ]
    }
    md, refs = render_body(body, lists={})
    assert "![](images/img_1.png)" in md
    assert "![](images/img_2.png)" in md
    assert [r.inline_object_id for r in refs] == ["kix.1", "kix.2"]


def test_inline_image_without_id_is_dropped() -> None:
    body = {
        "content": [
            {
                "paragraph": {
                    "elements": [
                        {"textRun": {"content": "x", "textStyle": {}}},
                        {"inlineObjectElement": {}},  # no inlineObjectId
                    ]
                }
            }
        ]
    }
    md, refs = render_body(body, lists={})
    assert md == "x"
    assert refs == []


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def test_has_tabs_true_only_when_tabs_present() -> None:
    assert has_tabs({"tabs": [{"tabProperties": {"tabId": "t.0"}}]})
    assert not has_tabs({"tabs": []})
    assert not has_tabs({})


def test_render_document_tabs_walks_children_in_order() -> None:
    document = {
        "tabs": [
            {
                "tabProperties": {"tabId": "t.root", "title": "Root"},
                "documentTab": {"body": {"content": [_para("root body")]}},
                "childTabs": [
                    {
                        "tabProperties": {"tabId": "t.child", "title": "Child"},
                        "documentTab": {"body": {"content": [_para("child body")]}},
                    }
                ],
            },
            {
                "tabProperties": {"tabId": "t.sibling", "title": "Sibling"},
                "documentTab": {"body": {"content": [_para("sibling body")]}},
            },
        ]
    }
    rendered = render_document_tabs(document)
    assert [r.title for r in rendered] == ["Root", "Child", "Sibling"]
    assert rendered[1].parent_titles == ["Root"]
    assert rendered[1].display_name == "Root > Child"
    # Bodies match
    assert "root body" in rendered[0].markdown
    assert "child body" in rendered[1].markdown
    assert "sibling body" in rendered[2].markdown


def test_render_document_tabs_threads_lists_map() -> None:
    """The top-level ``document.lists`` map must reach the body walker so
    ordered-list detection works for every tab."""
    document = {
        "lists": _ordered_list_spec("L1"),
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "Only"},
                "documentTab": {
                    "body": {
                        "content": [
                            _bullet("a"),
                            _bullet("b"),
                        ]
                    }
                },
            }
        ],
    }
    rendered = render_document_tabs(document)
    assert "1. a" in rendered[0].markdown
    assert "2. b" in rendered[0].markdown


def test_image_references_isolated_per_tab() -> None:
    """Image counters reset between tabs — each tab writes into its own
    ``images/`` subdirectory."""
    document = {
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "First"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {"inlineObjectElement": {"inlineObjectId": "a"}}
                                    ]
                                }
                            }
                        ]
                    }
                },
            },
            {
                "tabProperties": {"tabId": "t.1", "title": "Second"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {"inlineObjectElement": {"inlineObjectId": "b"}}
                                    ]
                                }
                            }
                        ]
                    }
                },
            },
        ]
    }
    rendered = render_document_tabs(document)
    assert rendered[0].image_references[0].rel_path == "images/img_1.png"
    assert rendered[1].image_references[0].rel_path == "images/img_1.png"
    assert rendered[0].image_references[0].inline_object_id == "a"
    assert rendered[1].image_references[0].inline_object_id == "b"
