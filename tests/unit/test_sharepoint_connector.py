"""Unit tests for the SharePoint connector — pure helpers + URL parser.

The async sync flow needs httpx mocking; this test file covers the
deterministic helpers so refactors don't accidentally change the
on-disk shape.
"""

from __future__ import annotations

import json

from voitta_rag_enterprise.services.sync.sharepoint import (
    SharePointConnector,
    coerce_sites_field,
    encode_sites_field,
    parse_sharepoint_url,
)


def test_coerce_sites_field_empty():
    assert coerce_sites_field(None) == []
    assert coerce_sites_field("") == []
    assert coerce_sites_field("not json") == []


def test_coerce_sites_field_filters_invalid():
    raw = json.dumps([
        {"id": "abc", "displayName": "Site A", "webUrl": "https://x/a"},
        {"displayName": "no id"},  # dropped
        "not a dict",  # dropped
        {"id": 123, "displayName": "coerced int id", "webUrl": ""},
    ])
    out = coerce_sites_field(raw)
    assert len(out) == 2
    assert out[0]["id"] == "abc"
    assert out[1]["id"] == "123"


def test_encode_sites_field_roundtrip():
    sites = [{"id": "x", "displayName": "X", "webUrl": "https://x"}]
    encoded = encode_sites_field(sites)
    assert encoded is not None
    assert coerce_sites_field(encoded) == sites


def test_encode_sites_field_empty():
    assert encode_sites_field(None) is None
    assert encode_sites_field([]) is None


def test_parse_sharepoint_url_site():
    host, site, drive = parse_sharepoint_url(
        "https://contoso.sharepoint.com/sites/Marketing"
    )
    assert host == "contoso.sharepoint.com"
    assert site == "/sites/Marketing"
    assert drive == ""


def test_parse_sharepoint_url_subfolder():
    host, site, drive = parse_sharepoint_url(
        "https://contoso.sharepoint.com/sites/Marketing/Shared%20Documents/Q4"
    )
    assert host == "contoso.sharepoint.com"
    assert site == "/sites/Marketing"
    assert drive == "Q4"


def test_parse_sharepoint_url_forms_aspx_stripped():
    host, site, drive = parse_sharepoint_url(
        "https://x.sharepoint.com/sites/X/Shared%20Documents/Forms/AllItems.aspx"
    )
    assert site == "/sites/X"
    assert drive == ""


def test_connector_source_type():
    assert SharePointConnector.source_type == "sharepoint"
