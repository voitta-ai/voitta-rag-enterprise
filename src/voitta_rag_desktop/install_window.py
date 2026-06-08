"""First-launch installation progress window (PyObjC NSWindow).

Shown by the menu-bar shell before uvicorn starts, whenever the first-run
installer has work to do. Three phase rows are displayed vertically, matching
``installer._STEPS``:

  Phase 0 — Python packages   (lazy pip install of the heavy ML/server stack)
  Phase 1 — Search engine     (download the managed Qdrant binary)
  Phase 2 — AI models         (prewarm the e5 / SigLIP embedders)

A scrolling log below the rows streams the raw pip / download output so the
user can see exactly what is happening.

Cocoa is single-threaded: every UI mutation from the installer's worker thread
MUST go through ``PyObjCTools.AppHelper.callAfter``. Every public method here
wraps the real work in ``callAfter`` so callers don't have to care.
"""

from __future__ import annotations

from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSForegroundColorAttributeName,
    NSProgressIndicator,
    NSProgressIndicatorStyleBar,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSWindow,
    NSWindowStyleMaskTitled,
)
from Foundation import NSAttributedString
from PyObjCTools import AppHelper

_PHASE_NAMES = ["Python packages", "Search engine", "AI models"]
_WINDOW_W = 600
_WINDOW_H = 500

# Y positions (Cocoa origin = bottom-left) for each phase row. Each row occupies
# ~72px: name(20) + bar(16) + status(18) + padding.
_PHASE_Y = [340, 268, 196]


class _PhaseRow:
    """One row: name label (with ◦/▶/✓/↷/✗ prefix) + progress bar + status."""

    def __init__(self, cv, y: int, name: str) -> None:
        w = _WINDOW_W - 40

        self._name = NSTextField.labelWithString_(f"◦  {name}")
        self._name.setFrame_(((20, y + 48), (w, 20)))
        self._name.setFont_(NSFont.boldSystemFontOfSize_(13))
        cv.addSubview_(self._name)

        self._bar = NSProgressIndicator.alloc().initWithFrame_(((20, y + 26), (w, 16)))
        self._bar.setStyle_(NSProgressIndicatorStyleBar)
        self._bar.setIndeterminate_(False)
        self._bar.setMinValue_(0.0)
        self._bar.setMaxValue_(1.0)
        self._bar.setDoubleValue_(0.0)
        self._bar.setUsesThreadedAnimation_(True)
        cv.addSubview_(self._bar)

        self._status = NSTextField.labelWithString_("Waiting…")
        self._status.setFrame_(((20, y + 4), (w, 18)))
        self._status.setFont_(NSFont.systemFontOfSize_(11))
        self._status.setTextColor_(NSColor.secondaryLabelColor())
        cv.addSubview_(self._status)

        self._raw_name = name

    def activate(self, label: str = "Running…") -> None:
        self._name.setStringValue_(f"▶  {self._raw_name}")
        self._name.setTextColor_(NSColor.labelColor())
        self._status.setStringValue_(label)
        self._status.setTextColor_(NSColor.secondaryLabelColor())
        self._bar.setIndeterminate_(True)
        self._bar.startAnimation_(None)

    def update_progress(self, current: int, total: int, label: str) -> None:
        self._bar.stopAnimation_(None)
        self._bar.setIndeterminate_(False)
        self._bar.setMaxValue_(float(max(total, 1)))
        self._bar.setDoubleValue_(float(current))
        self._status.setStringValue_(label)

    def done(self, note: str = "Done") -> None:
        self._bar.stopAnimation_(None)
        self._bar.setIndeterminate_(False)
        self._bar.setMaxValue_(1.0)
        self._bar.setDoubleValue_(1.0)
        self._name.setStringValue_(f"✓  {self._raw_name}")
        self._name.setTextColor_(NSColor.systemGreenColor())
        self._status.setStringValue_(note)
        self._status.setTextColor_(NSColor.secondaryLabelColor())

    def skip(self, note: str = "Already installed") -> None:
        self._bar.stopAnimation_(None)
        self._bar.setIndeterminate_(False)
        self._bar.setMaxValue_(1.0)
        self._bar.setDoubleValue_(1.0)
        self._name.setStringValue_(f"↷  {self._raw_name}")
        self._name.setTextColor_(NSColor.secondaryLabelColor())
        self._status.setStringValue_(note)

    def fail(self, reason: str) -> None:
        self._bar.stopAnimation_(None)
        self._name.setStringValue_(f"✗  {self._raw_name}")
        self._name.setTextColor_(NSColor.systemRedColor())
        self._status.setStringValue_(reason[:100])
        self._status.setTextColor_(NSColor.systemRedColor())


class InstallWindow:
    """Title-bar-only setup window with 3 phase rows and a shared log.

    No close/minimize/zoom buttons (style mask is Titled only) so the user
    cannot dismiss it mid-setup.
    """

    def __init__(self) -> None:
        rect = ((0, 0), (_WINDOW_W, _WINDOW_H))
        self._w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        self._w.setTitle_("Voitta RAG — First-Run Setup")
        self._w.setReleasedWhenClosed_(False)
        self._w.center()

        cv = self._w.contentView()

        title = NSTextField.labelWithString_("Setting up Voitta RAG for the first time")
        title.setFrame_(((20, _WINDOW_H - 44), (_WINDOW_W - 40, 26)))
        title.setFont_(NSFont.boldSystemFontOfSize_(15))
        cv.addSubview_(title)

        subtitle = NSTextField.labelWithString_(
            "Downloading the search engine, ML stack and models. "
            "This window closes automatically when ready."
        )
        subtitle.setFrame_(((20, _WINDOW_H - 66), (_WINDOW_W - 40, 18)))
        subtitle.setFont_(NSFont.systemFontOfSize_(11))
        subtitle.setTextColor_(NSColor.secondaryLabelColor())
        cv.addSubview_(subtitle)

        sep = NSTextField.labelWithString_("")
        sep.setFrame_(((20, _WINDOW_H - 74), (_WINDOW_W - 40, 1)))
        sep.setBackgroundColor_(NSColor.separatorColor())
        sep.setDrawsBackground_(True)
        cv.addSubview_(sep)

        self._rows = [
            _PhaseRow(cv, y, name) for y, name in zip(_PHASE_Y, _PHASE_NAMES)
        ]

        # Scrolling monospace log.
        log_y = 50
        log_h = _PHASE_Y[2] - log_y - 12
        scroll = NSScrollView.alloc().initWithFrame_(
            ((20, log_y), (_WINDOW_W - 40, log_h))
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)  # NSBezelBorder
        log = NSTextView.alloc().initWithFrame_(((0, 0), (_WINDOW_W - 40, log_h)))
        log.setEditable_(False)
        log.setFont_(NSFont.userFixedPitchFontOfSize_(10))
        log.setBackgroundColor_(NSColor.textBackgroundColor())
        log.setTextColor_(NSColor.labelColor())
        self._log_attrs = {NSForegroundColorAttributeName: NSColor.labelColor()}
        log.setTypingAttributes_(self._log_attrs)
        scroll.setDocumentView_(log)
        cv.addSubview_(scroll)
        self._log = log

        footer = NSTextField.labelWithString_(
            "One-time setup — a few minutes on a fast connection. "
            "Please don't close this window."
        )
        footer.setFrame_(((20, 20), (_WINDOW_W - 40, 22)))
        footer.setFont_(NSFont.systemFontOfSize_(10))
        footer.setTextColor_(NSColor.tertiaryLabelColor())
        cv.addSubview_(footer)

    # ---- public API (all thread-safe via callAfter) ------------------------

    def show(self) -> None:
        AppHelper.callAfter(self._show_impl)

    def _show_impl(self) -> None:
        from AppKit import NSApp

        self._w.makeKeyAndOrderFront_(None)
        self._w.center()
        NSApp.activateIgnoringOtherApps_(True)

    def start_phase(self, phase: int, label: str = "Running…") -> None:
        AppHelper.callAfter(self._rows[phase].activate, label)

    def update_phase(self, phase: int, current: int, total: int, label: str) -> None:
        AppHelper.callAfter(self._rows[phase].update_progress, current, total, label)

    def finish_phase(self, phase: int, note: str = "Done") -> None:
        AppHelper.callAfter(self._rows[phase].done, note)

    def skip_phase(self, phase: int, note: str = "Already installed") -> None:
        AppHelper.callAfter(self._rows[phase].skip, note)

    def fail_phase(self, phase: int, reason: str) -> None:
        AppHelper.callAfter(self._rows[phase].fail, reason)

    def log(self, line: str) -> None:
        AppHelper.callAfter(self._log_impl, line)

    def _log_impl(self, line: str) -> None:
        ts = self._log.textStorage()
        ts.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                line + "\n", self._log_attrs
            )
        )
        self._log.scrollRangeToVisible_((ts.length(), 0))

    def close(self) -> None:
        AppHelper.callAfter(self._w.orderOut_, None)
