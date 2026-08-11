"""execute_with_retry — backoff on Workspace rate-limit / transient errors.

Regression guard for the Google Docs sync flooding the Docs API past its
300-reads/min/user quota: before this, a 429 on documents.get was never
retried, so every overflow doc landed as an empty/failed export. The helper
now retries 429 (and transient 5xx) with backoff, leaving genuine 403
permission errors to fail fast.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from voitta_rag_enterprise.services.sync.google_workspace_exporters.base import (
    execute_with_retry,
)


class _FakeResp(dict):
    """Mimics httplib2.Response: a dict (header access) with a .status."""

    def __init__(self, status: int, headers: dict | None = None) -> None:
        super().__init__(headers or {})
        self.status = status
        self.reason = "rate-limit"  # HttpError.__str__ reads this


def _http_error(status: int, reason: str = "", headers: dict | None = None) -> HttpError:
    content = (
        b'{"error": {"errors": [{"reason": "%s"}], "message": "%s"}}'
        % (reason.encode(), reason.encode())
    )
    return HttpError(_FakeResp(status, headers), content)


class _Request:
    """A googleapiclient-shaped request: raises a queued series, then returns."""

    def __init__(self, errors: list[HttpError], result: object = "ok") -> None:
        self._errors = list(errors)
        self._result = result
        self.calls = 0

    def execute(self) -> object:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._result


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the backoff logic but make every sleep instant.
    monkeypatch.setattr(
        "voitta_rag_enterprise.services.sync.google_workspace_exporters.base.time.sleep",
        lambda _s: None,
    )


def test_succeeds_first_try() -> None:
    req = _Request([])
    assert execute_with_retry(req) == "ok"
    assert req.calls == 1


def test_retries_429_then_succeeds() -> None:
    req = _Request([_http_error(429, "rateLimitExceeded")] * 3, result="doc")
    assert execute_with_retry(req) == "doc"
    assert req.calls == 4  # 3 failures + 1 success


def test_429_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "voitta_rag_enterprise.services.sync.google_workspace_exporters.base.time.sleep",
        lambda s: slept.append(s),
    )
    req = _Request([_http_error(429, headers={"retry-after": "7"})])
    execute_with_retry(req)
    assert slept == [7.0]


def test_exhausts_and_reraises() -> None:
    req = _Request([_http_error(429, "rateLimitExceeded")] * 10)
    with pytest.raises(HttpError):
        execute_with_retry(req, max_attempts=4)
    assert req.calls == 4


def test_permission_403_fails_fast() -> None:
    # A plain 403 (no rate-limit reason) is a hard error — no retry.
    req = _Request([_http_error(403, "insufficientPermissions")])
    with pytest.raises(HttpError):
        execute_with_retry(req)
    assert req.calls == 1


def test_user_rate_limit_403_is_retried() -> None:
    # Drive reports user-rate overruns as 403 userRateLimitExceeded.
    req = _Request([_http_error(403, "userRateLimitExceeded")], result="ok")
    assert execute_with_retry(req) == "ok"
    assert req.calls == 2


def test_transient_500_is_retried() -> None:
    req = _Request([_http_error(500, "backendError")], result="ok")
    assert execute_with_retry(req) == "ok"
    assert req.calls == 2


def test_non_retryable_404_fails_fast() -> None:
    req = _Request([_http_error(404, "notFound")])
    with pytest.raises(HttpError):
        execute_with_retry(req)
    assert req.calls == 1


# ---------------------------------------------------------------------------
# Sheets pacing — the 60-reads/min/user quota is shared across every worker
# thread, so ``label="sheets"`` requests reserve evenly-spaced slots on a
# process-wide clock before executing. Regression guard for the Agnitio
# Drive sync flooding sheets.googleapis.com from 8 parallel download
# threads: per-thread backoff alone kept collectively exceeding the quota
# until files exhausted their retries and landed in ``stats.errors``.
# ---------------------------------------------------------------------------

_BASE = "voitta_rag_enterprise.services.sync.google_workspace_exporters.base"


def test_sheets_label_paces_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retries consume quota like first attempts — each must take a slot.
    paced: list[int] = []
    monkeypatch.setattr(f"{_BASE}._sheets_pace", lambda: paced.append(1))
    req = _Request([_http_error(429, "rateLimitExceeded")] * 2, result="ok")
    assert execute_with_retry(req, label="sheets") == "ok"
    assert req.calls == 3
    assert len(paced) == 3  # one slot per attempt, not one per request


def test_non_sheets_labels_are_not_paced(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docs/Slides/Forms (300/min) and Drive don't need pacing — only the
    # sheets label pays the gate.
    paced: list[int] = []
    monkeypatch.setattr(f"{_BASE}._sheets_pace", lambda: paced.append(1))
    req = _Request([])
    assert execute_with_retry(req) == "ok"
    assert execute_with_retry(_Request([]), label="docs") == "ok"
    assert paced == []


def test_sheets_pace_enforces_min_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.services.sync.google_workspace_exporters import (
        base as base_mod,
    )

    # Freeze the clock and capture sleeps; reset the module-global slot so
    # this test is order-independent.
    slept: list[float] = []
    monkeypatch.setattr(f"{_BASE}.time.monotonic", lambda: 1000.0)
    monkeypatch.setattr(f"{_BASE}.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr(f"{_BASE}._sheets_next_slot", 0.0)

    interval = 60.0 / base_mod._SHEETS_READS_PER_MIN
    base_mod._sheets_pace()  # first call: slot is now — no sleep
    base_mod._sheets_pace()  # second call: must wait one interval
    base_mod._sheets_pace()  # third call: two intervals out
    assert slept == pytest.approx([interval, 2 * interval])
