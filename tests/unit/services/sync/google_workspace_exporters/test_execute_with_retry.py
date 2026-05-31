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
