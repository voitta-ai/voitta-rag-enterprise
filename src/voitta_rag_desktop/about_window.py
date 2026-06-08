"""About window (PyObjC NSWindow) for the Voitta RAG desktop app.

A proper titled + closable window — real red traffic-light close button and
Escape-to-close — rather than an NSAlert, so it gets native window chrome and a
clean custom layout. Shows version / status / paths in an aligned two-column
block and the MCP client config in a bordered monospace box with a Copy button.

All construction must happen on the main (AppKit) thread; the caller (a menu
action) already runs there. The controller object is returned so the caller can
retain it for the window's lifetime — buttons target it.
"""

from __future__ import annotations

import webbrowser

from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSScrollView,
    NSTextAlignmentRight,
    NSTextField,
    NSTextView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from objc import IBAction, super  # noqa: A004

_W = 660
_MARGIN = 28
_LABEL_W = 120
_LINE_H = 19
_WEBSITE = "https://voitta.ai"


class _EscWindow(NSWindow):
    """NSWindow that closes on Escape — the responder chain sends
    ``cancelOperation:`` for the Esc key."""

    def cancelOperation_(self, sender):  # noqa: N802
        self.performClose_(sender)

    def canBecomeKeyWindow(self):  # noqa: N802
        return True


class _ButtonTarget(NSObject):
    """Reusable ObjC target so plain-Python callbacks can back NSButtons."""

    def initWithCallback_(self, cb):  # noqa: N802
        self = super().init()
        if self is None:
            return None
        self._cb = cb
        return self

    @IBAction
    def invoke_(self, sender):  # noqa: N802
        self._cb()


class AboutController:
    """Builds + owns the About window."""

    def __init__(self, *, rows, config_json, on_copy) -> None:
        self._config = config_json
        self._on_copy = on_copy

        rows_h = len(rows) * _LINE_H
        height = (
            _MARGIN              # top margin
            + 26 + 16            # title + gap
            + rows_h + 18        # metadata block + gap
            + 18 + 18 + 8        # header + note + gap
            + 150 + 20           # JSON box + gap
            + 32 + _MARGIN       # buttons + bottom margin
        )
        self._h = height

        win = _EscWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (_W, height)),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        win.setTitle_("About Voitta RAG")
        win.setReleasedWhenClosed_(False)
        win.center()
        self._win = win
        cv = win.contentView()

        self._cy = _MARGIN  # cursor: distance consumed from the top edge

        # ---- title --------------------------------------------------------
        title = NSTextField.labelWithString_("Voitta RAG")
        title.setFont_(NSFont.boldSystemFontOfSize_(18))
        self._place(cv, title, _MARGIN, _W - 2 * _MARGIN, 26)
        self._gap(16)

        # ---- metadata: aligned two columns --------------------------------
        for label, value in rows:
            top = self._cy
            lab = NSTextField.labelWithString_(label)
            lab.setFont_(NSFont.systemFontOfSize_(12))
            lab.setTextColor_(NSColor.secondaryLabelColor())
            lab.setAlignment_(NSTextAlignmentRight)
            self._place_at(cv, lab, _MARGIN, _LABEL_W, _LINE_H, top)

            val = NSTextField.labelWithString_(value)
            val.setFont_(NSFont.systemFontOfSize_(12))
            val.setSelectable_(True)
            vx = _MARGIN + _LABEL_W + 14
            self._place_at(cv, val, vx, _W - vx - _MARGIN, _LINE_H, top)
            self._cy += _LINE_H
        self._gap(18)

        # ---- "Connect Claude" header + note -------------------------------
        hdr = NSTextField.labelWithString_("Connect Claude")
        hdr.setFont_(NSFont.boldSystemFontOfSize_(12))
        self._place(cv, hdr, _MARGIN, _W - 2 * _MARGIN, 18)
        note = NSTextField.labelWithString_(
            "Paste into .mcp.json / claude_desktop_config.json — "
            "no token needed (local single-user access)."
        )
        note.setFont_(NSFont.systemFontOfSize_(11))
        note.setTextColor_(NSColor.secondaryLabelColor())
        self._place(cv, note, _MARGIN, _W - 2 * _MARGIN, 18)
        self._gap(8)

        # ---- monospace JSON box -------------------------------------------
        box_h = 150
        top = self._cy
        scroll = NSScrollView.alloc().initWithFrame_(
            ((_MARGIN, height - top - box_h), (_W - 2 * _MARGIN, box_h))
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)  # NSBezelBorder
        tv = NSTextView.alloc().initWithFrame_(((0, 0), (_W - 2 * _MARGIN, box_h)))
        tv.setEditable_(False)
        tv.setFont_(NSFont.userFixedPitchFontOfSize_(12))
        tv.setString_(config_json)
        tv.setTextContainerInset_((8, 8))
        scroll.setDocumentView_(tv)
        cv.addSubview_(scroll)
        self._cy += box_h
        self._gap(20)

        # ---- buttons (bottom-right): Copy (default) + Open Website --------
        self._copy_t = _ButtonTarget.alloc().initWithCallback_(self._do_copy)
        self._open_t = _ButtonTarget.alloc().initWithCallback_(self._do_open)
        by = _MARGIN  # from the bottom edge
        copy_btn = self._button("Copy MCP Config", 200, self._copy_t)
        copy_btn.setKeyEquivalent_("\r")  # default (blue); Return triggers it
        copy_btn.setFrame_(((_W - _MARGIN - 200, by), (200, 32)))
        cv.addSubview_(copy_btn)
        open_btn = self._button("Open Website", 150, self._open_t)
        open_btn.setFrame_(((_W - _MARGIN - 200 - 12 - 150, by), (150, 32)))
        cv.addSubview_(open_btn)

    # ---- layout helpers ----------------------------------------------------

    def _place(self, cv, view, x, w, h) -> None:
        view.setFrame_(((x, self._h - self._cy - h), (w, h)))
        cv.addSubview_(view)
        self._cy += h

    def _place_at(self, cv, view, x, w, h, top) -> None:
        view.setFrame_(((x, self._h - top - h), (w, h)))
        cv.addSubview_(view)

    def _gap(self, n) -> None:
        self._cy += n

    def _button(self, title, w, target):
        b = NSButton.alloc().initWithFrame_(((0, 0), (w, 32)))
        b.setTitle_(title)
        b.setBezelStyle_(NSBezelStyleRounded)
        b.setTarget_(target)
        b.setAction_("invoke:")
        return b

    # ---- actions -----------------------------------------------------------

    def _do_copy(self) -> None:
        if self._on_copy:
            self._on_copy(self._config)

    def _do_open(self) -> None:
        webbrowser.open(_WEBSITE)

    # ---- show --------------------------------------------------------------

    def show(self) -> None:
        self._win.makeKeyAndOrderFront_(None)
        self._win.center()
        NSApp.activateIgnoringOtherApps_(True)
