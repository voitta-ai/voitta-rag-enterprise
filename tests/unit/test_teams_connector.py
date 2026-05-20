"""Unit tests for the Teams connector helpers."""

from __future__ import annotations

from voitta_rag_enterprise.services.sync.teams import (
    TeamsConnector,
    _parse_iso_epoch,
    _recording_links,
    _loop_links,
    _user_in_call_record,
)


def test_connector_source_type():
    assert TeamsConnector.source_type == "teams"


def test_parse_iso_epoch_zulu():
    epoch = _parse_iso_epoch("2026-01-01T00:00:00Z")
    assert epoch is not None
    assert isinstance(epoch, float)


def test_parse_iso_epoch_offset():
    epoch = _parse_iso_epoch("2026-01-01T00:00:00+02:00")
    assert epoch is not None


def test_parse_iso_epoch_empty():
    assert _parse_iso_epoch("") is None
    assert _parse_iso_epoch("not a date") is None


def test_recording_links_handles_missing():
    assert _recording_links({}) == []
    assert _recording_links({"recordings": []}) == []


def test_recording_links_extracts():
    out = _recording_links({
        "recordings": [
            {"subject": "demo", "recordingContentUrl": "https://x/r"},
            {"url": "https://x/r2"},
            {"subject": "no url"},  # dropped
        ]
    })
    assert len(out) == 2
    assert out[0]["url"] == "https://x/r"


def test_loop_links_filters_by_keyword():
    out = _loop_links({
        "attachments": [
            {"title": "loop", "url": "https://contoso-my.sharepoint.com/personal/x/loop/file.fluid"},
            {"title": "ignore", "url": "https://x/normal.docx"},
            {"title": "doc.aspx", "url": "https://x/_layouts/15/Doc.aspx?id=foo"},
        ]
    })
    assert len(out) == 2
    titles = {item["title"] for item in out}
    assert "loop" in titles
    assert "doc.aspx" in titles


def test_user_in_call_record_participant():
    rec = {"participants_v2": [{"user": {"id": "user-1"}}]}
    assert _user_in_call_record(rec, "user-1") is True
    assert _user_in_call_record(rec, "user-2") is False


def test_user_in_call_record_organizer():
    rec = {"organizer_v2": {"user": {"id": "user-3"}}}
    assert _user_in_call_record(rec, "user-3") is True
