"""scanner.load_sidecar — parsing the extended .voitta_sources.json records."""

from __future__ import annotations

import json
from pathlib import Path

from voitta_rag_enterprise.services.scanner import SIDECAR_FILENAME, load_sidecar


def test_load_sidecar_parses_url_tab_and_meta(tmp_path: Path) -> None:
    (tmp_path / SIDECAR_FILENAME).write_text(json.dumps({
        "a.pdf": {
            "url": "https://drive/a", "tab": None,
            "owner_email": "roman@x.com", "owner_name": "Roman",
            "shared_by_email": "grp@x.com",
            "created_ts": 1700000000, "modified_ts": 1700500000,
            "garbage_future_key": "ignored",   # forward-compat: unknown keys dropped
        },
        "b.txt": {"url": "https://drive/b"},     # no meta → meta is None
    }))
    out = load_sidecar(tmp_path)

    a = out["a.pdf"]
    assert a.url == "https://drive/a"
    assert a.meta == {
        "owner_email": "roman@x.com", "owner_name": "Roman",
        "shared_by_email": "grp@x.com",
        "created_ts": 1700000000, "modified_ts": 1700500000,
    }
    assert "garbage_future_key" not in a.meta

    assert out["b.txt"].meta is None


def test_load_sidecar_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_sidecar(tmp_path) == {}
