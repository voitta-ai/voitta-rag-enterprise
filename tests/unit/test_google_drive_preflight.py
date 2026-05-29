"""Preflight: probe every Workspace API once and fail fast on
SERVICE_DISABLED / insufficient OAuth scope.

We construct a real :class:`googleapiclient.errors.HttpError` per probe
so the production-side detection helpers (``_is_service_disabled``,
``_is_scope_insufficient``, ``_extract_activation_url``) run against
the same shape Google would return.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from voitta_rag_enterprise.services.sync.google_drive import (
    GoogleWorkspaceAccessError,
    preflight_drive_only,
    preflight_workspace_apis,
    probe_workspace_apis,
)


# ---------------------------------------------------------------------------
# Error fixtures
# ---------------------------------------------------------------------------


def _http_error(*, status: int, body: dict | None = None, raw: bytes | None = None) -> HttpError:
    resp = Response({"status": status})
    resp.status = status
    content = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
    return HttpError(resp, content)


def _service_disabled(api_name: str) -> HttpError:
    return _http_error(
        status=403,
        body={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": f"{api_name} has not been used in project 12345 before or it is disabled.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "SERVICE_DISABLED",
                        "domain": "googleapis.com",
                        "metadata": {
                            "service": api_name,
                            "consumer": "projects/12345",
                            "activationUrl": (
                                f"https://console.developers.google.com/apis/api/"
                                f"{api_name}/overview?project=12345"
                            ),
                        },
                    }
                ],
            }
        },
    )


def _not_found() -> HttpError:
    return _http_error(
        status=404,
        body={"error": {"code": 404, "status": "NOT_FOUND", "message": "Requested entity was not found."}},
    )


def _scope_insufficient() -> HttpError:
    return _http_error(
        status=403,
        body={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "Request had insufficient authentication scopes.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                        "domain": "googleapis.com",
                    }
                ],
            }
        },
    )


# ---------------------------------------------------------------------------
# Service stubs
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, error: HttpError | None = None) -> None:
        self._error = error

    def execute(self) -> dict:
        if self._error is not None:
            raise self._error
        return {"id": "ok"}


def _drive_stub(error: HttpError | None) -> Any:
    """Returns an object whose ``files().get(...)`` raises ``error``."""

    class _Files:
        def get(self, *, fileId: str, supportsAllDrives: bool = True) -> _FakeRequest:  # noqa: ARG002
            return _FakeRequest(error)

    class _Drive:
        def files(self):
            return _Files()

    return _Drive()


def _docs_stub(error: HttpError | None) -> Any:
    class _Docs:
        def documents(self):
            return self

        def get(self, *, documentId: str) -> _FakeRequest:  # noqa: ARG002
            return _FakeRequest(error)

    return _Docs()


def _sheets_stub(error: HttpError | None) -> Any:
    class _Sheets:
        def spreadsheets(self):
            class _SS:
                def get(self, *, spreadsheetId: str) -> _FakeRequest:  # noqa: ARG002
                    return _FakeRequest(error)

            return _SS()

    return _Sheets()


def _slides_stub(error: HttpError | None) -> Any:
    class _Slides:
        def presentations(self):
            class _P:
                def get(self, *, presentationId: str) -> _FakeRequest:  # noqa: ARG002
                    return _FakeRequest(error)

            return _P()

    return _Slides()


def _forms_stub(error: HttpError | None) -> Any:
    class _Forms:
        def forms(self):
            return self

        def get(self, *, formId: str) -> _FakeRequest:  # noqa: ARG002
            return _FakeRequest(error)

    return _Forms()


def _services(*, drive=None, docs=None, sheets=None, slides=None, forms=None) -> dict[str, Any]:
    """Build the five-service kwargs dict; each arg is the
    HttpError each probe should raise (None = enabled / 404)."""
    return {
        "drive": _drive_stub(drive if isinstance(drive, HttpError) else _not_found() if drive is None else drive),
        "docs": _docs_stub(docs if isinstance(docs, HttpError) else _not_found() if docs is None else docs),
        "sheets": _sheets_stub(sheets if isinstance(sheets, HttpError) else _not_found() if sheets is None else sheets),
        "slides": _slides_stub(slides if isinstance(slides, HttpError) else _not_found() if slides is None else slides),
        "forms": _forms_stub(forms if isinstance(forms, HttpError) else _not_found() if forms is None else forms),
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_preflight_passes_when_every_api_returns_404() -> None:
    """All five APIs respond 404 NOT_FOUND for the bogus id → enabled.
    Preflight is a no-op."""
    preflight_workspace_apis(**_services())  # no exception


def test_preflight_passes_when_apis_return_400_invalid_argument() -> None:
    """Sheets historically returns 400 INVALID_ARGUMENT for malformed
    ids rather than 404. That's still "API responded" → enabled."""
    bad_arg = _http_error(
        status=400,
        body={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad id"}},
    )
    preflight_workspace_apis(**_services(sheets=bad_arg))


# ---------------------------------------------------------------------------
# SERVICE_DISABLED
# ---------------------------------------------------------------------------


def test_preflight_raises_with_disabled_apis_listed() -> None:
    err = _service_disabled("docs.googleapis.com")
    with pytest.raises(GoogleWorkspaceAccessError) as ei:
        preflight_workspace_apis(**_services(docs=err))
    msg = str(ei.value)
    assert "Google Docs" in msg
    assert "docs.googleapis.com" in msg
    assert ei.value.disabled_apis  # populated
    assert ei.value.disabled_apis[0][0] == "Google Docs"
    assert ei.value.disabled_apis[0][1].startswith("https://console")


def test_preflight_aggregates_multiple_disabled_apis() -> None:
    docs_err = _service_disabled("docs.googleapis.com")
    sheets_err = _service_disabled("sheets.googleapis.com")
    slides_err = _service_disabled("slides.googleapis.com")
    with pytest.raises(GoogleWorkspaceAccessError) as ei:
        preflight_workspace_apis(
            **_services(docs=docs_err, sheets=sheets_err, slides=slides_err)
        )
    names = {label for label, _ in ei.value.disabled_apis}
    assert names == {"Google Docs", "Google Sheets", "Google Slides"}
    assert ei.value.scope_problem is False


def test_preflight_extracts_activation_url_from_error_body() -> None:
    err = _service_disabled("forms.googleapis.com")
    with pytest.raises(GoogleWorkspaceAccessError) as ei:
        preflight_workspace_apis(**_services(forms=err))
    label, url = ei.value.disabled_apis[0]
    assert label == "Google Forms"
    assert "forms.googleapis.com/overview" in url
    assert "project=12345" in url


def test_preflight_falls_back_to_library_url_when_metadata_missing() -> None:
    """Older Google responses don't carry ``activationUrl``; we synthesise
    the standard ``apis/library`` URL so the user still has somewhere to
    click."""
    err = _http_error(
        status=403,
        body={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "API not enabled",
                "details": [{"reason": "SERVICE_DISABLED"}],  # no metadata
            }
        },
    )
    with pytest.raises(GoogleWorkspaceAccessError) as ei:
        preflight_workspace_apis(**_services(sheets=err))
    _, url = ei.value.disabled_apis[0]
    assert url == "https://console.cloud.google.com/apis/library/sheets.googleapis.com"


# ---------------------------------------------------------------------------
# Insufficient scope
# ---------------------------------------------------------------------------


def test_preflight_detects_insufficient_scope() -> None:
    """OAuth refresh_token predates the scope expansion → re-consent
    needed. We surface this distinctly from SERVICE_DISABLED."""
    with pytest.raises(GoogleWorkspaceAccessError) as ei:
        preflight_workspace_apis(**_services(sheets=_scope_insufficient()))
    assert ei.value.scope_problem is True
    assert ei.value.disabled_apis == []
    assert "Reconnect" in str(ei.value)


def test_preflight_scope_problem_short_circuits_remaining_probes() -> None:
    """Once we see a scope-insufficient error on any one API, every
    subsequent probe will fail the same way. We stop the loop and only
    report the scope problem (avoids spurious 403s in logs)."""
    sentinel: list[str] = []

    class _RecordingDocs:
        def documents(self):
            sentinel.append("docs.documents")
            return self

        def get(self, *, documentId: str):  # noqa: ARG002
            sentinel.append("docs.get")
            return _FakeRequest(_scope_insufficient())

    class _RecordingForms:
        def forms(self):
            sentinel.append("forms.forms")
            return self

        def get(self, *, formId: str):  # noqa: ARG002
            sentinel.append("forms.get")  # should never be reached
            return _FakeRequest(_not_found())

    services = _services()
    services["docs"] = _RecordingDocs()
    services["forms"] = _RecordingForms()
    with pytest.raises(GoogleWorkspaceAccessError):
        preflight_workspace_apis(**services)
    # Forms was after Docs in the probe order — should be skipped.
    assert "forms.get" not in sentinel
    assert "docs.get" in sentinel


# ---------------------------------------------------------------------------
# probe_workspace_apis — structured, non-raising
# ---------------------------------------------------------------------------


def test_probe_reports_all_enabled() -> None:
    st = probe_workspace_apis(**_services())
    assert (st.drive, st.docs, st.sheets, st.slides, st.forms) == (
        True, True, True, True, True,
    )
    assert st.native_ok is True
    assert st.scope_problem is False
    assert st.disabled == []


def test_probe_reports_partial_disabled_without_raising() -> None:
    """Slides + Forms disabled (this deployment's real situation): probe
    flags exactly those two, Drive/Docs/Sheets stay up, and it does NOT
    raise — the caller decides what to do."""
    st = probe_workspace_apis(
        **_services(
            slides=_service_disabled("slides.googleapis.com"),
            forms=_service_disabled("forms.googleapis.com"),
        )
    )
    assert st.drive and st.docs and st.sheets
    assert st.slides is False and st.forms is False
    assert st.native_ok is False
    names = {label for label, _ in st.disabled}
    assert names == {"Google Slides", "Google Forms"}


def test_probe_flags_scope_problem() -> None:
    st = probe_workspace_apis(**_services(docs=_scope_insufficient()))
    assert st.scope_problem is True


# ---------------------------------------------------------------------------
# preflight_drive_only — files-only mode requires just Drive
# ---------------------------------------------------------------------------


def test_drive_only_passes_when_drive_enabled() -> None:
    """Drive responds 404 to the bogus id → enabled. No exception even
    though we never probe the (possibly disabled) native APIs."""
    preflight_drive_only(_drive_stub(_not_found()))


def test_drive_only_ignores_disabled_native_apis() -> None:
    """The whole point of files-only: Docs/Sheets/Slides/Forms can be
    disabled and Drive sync still proceeds. preflight_drive_only only
    looks at Drive, so it never even sees the others."""
    preflight_drive_only(_drive_stub(_not_found()))  # no native services passed


def test_drive_only_raises_when_drive_disabled() -> None:
    with pytest.raises(GoogleWorkspaceAccessError) as ei:
        preflight_drive_only(_drive_stub(_service_disabled("drive.googleapis.com")))
    assert ei.value.disabled_apis[0][0] == "Google Drive"
    assert "drive.googleapis.com" in str(ei.value)


def test_drive_only_raises_on_scope_problem() -> None:
    with pytest.raises(GoogleWorkspaceAccessError) as ei:
        preflight_drive_only(_drive_stub(_scope_insufficient()))
    assert ei.value.scope_problem is True