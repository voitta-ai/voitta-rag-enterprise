"""Tests for the Google Docs ``documents.get`` body → markdown renderer.

Fixtures here mirror real Docs API JSON shapes. We don't go to the network;
the renderer is pure-python over plain dicts.
"""

from __future__ import annotations

from voitta_image_rag.services.sync._gdoc_markdown import (
    has_tabs,
    render_body,
    render_document_tabs,
    safe_filename,
)


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


def test_render_body_emits_headings_and_paragraphs() -> None:
    body = {
        "content": [
            _para("Title here", named="HEADING_1"),
            _para("Body paragraph one."),
            _para("Body paragraph two."),
        ]
    }
    md = render_body(body)
    assert md.startswith("# Title here")
    assert "Body paragraph one." in md
    assert "Body paragraph two." in md
    # Headings should be on their own block, separated by a blank line.
    assert md.split("\n\n")[0] == "# Title here"


def test_render_body_groups_consecutive_bullets() -> None:
    body = {
        "content": [
            _bullet("First"),
            _bullet("Second"),
            _bullet("Nested", nesting=1),
            _para("After list."),
        ]
    }
    md = render_body(body)
    assert "- First" in md
    assert "- Second" in md
    assert "  - Nested" in md
    # The list block lives in a single paragraph; a separate "After list."
    # block follows it.
    assert "After list." in md.split("\n\n")[-1]


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
    md = render_body(body)
    lines = md.splitlines()
    assert lines[0] == "| a | b |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| c | d\\|with-pipe |"


def test_render_body_emphasis_bold_italic() -> None:
    elem_bold = {"textRun": {"content": "loud", "textStyle": {"bold": True}}}
    elem_plain = {"textRun": {"content": " quiet", "textStyle": {}}}
    body = {"content": [{"paragraph": {"elements": [elem_bold, elem_plain]}}]}
    md = render_body(body)
    assert md == "**loud** quiet"


def test_render_body_link_textstyle() -> None:
    elem = {
        "textRun": {
            "content": "voitta",
            "textStyle": {"link": {"url": "https://example.invalid/v"}},
        }
    }
    body = {"content": [{"paragraph": {"elements": [elem]}}]}
    md = render_body(body)
    assert md == "[voitta](https://example.invalid/v)"


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


def test_safe_filename_strips_path_chars() -> None:
    assert safe_filename("Hello / World?") == "Hello - World-"
    assert safe_filename("") == "tab"
    long = "x" * 500
    assert len(safe_filename(long)) == 80
