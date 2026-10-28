"""File.error keeps the end of a long traceback, where the reason is."""

from voitta_rag_enterprise.services.indexing.common import _ERROR_FIELD_MAX, _format_exception


def _fail() -> None:
    try:
        # A long cause, printed first: the traceback outgrows the field.
        raise RuntimeError("response body " + "x" * (2 * _ERROR_FIELD_MAX))
    except RuntimeError as e:
        raise ValueError("Vector dimension error: expected dim: 768, got 64") from e


def test_long_traceback_keeps_the_exception_message() -> None:
    try:
        _fail()
    except ValueError:
        msg = _format_exception("inline text embed failed")
    assert len(msg) <= _ERROR_FIELD_MAX
    assert msg.startswith("inline text embed failed\n")
    assert msg.rstrip().endswith("ValueError: Vector dimension error: expected dim: 768, got 64")


def test_short_traceback_is_kept_whole() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        msg = _format_exception("parse failed")
    assert msg.startswith("parse failed\nTraceback")
    assert "..." not in msg
