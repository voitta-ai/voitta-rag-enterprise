"""GET /api/files/{id}/layout over a MinerU 4 page layout.

MinerU 4 emits headings as ``type: "text"`` with an int ``text_level`` (no
``title`` type), so the route must pass ``text_level`` through or the SPA's
layout view cannot tell a heading from body text.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.cas import store as cas_store
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import File

from .test_acl_search import _seed_authed

_BLOCKS = [
    {"type": "text", "text": "Annual Operating Review", "text_level": 2, "bbox": [316, 92, 682, 119], "page_idx": 0, "page": 1},
    {"type": "text", "text": "Body paragraph.", "bbox": [109, 160, 881, 210], "page_idx": 0, "page": 1},
    {"type": "chart", "img_path": "images/page_0_chart_3.jpg", "bbox": [254, 236, 770, 477], "page_idx": 0, "page": 1},
    {"type": "text", "text": "odd", "text_level": True, "page_idx": 1, "page": 2},
    {"type": "list", "list_items": ["a"], "page_idx": 1, "page": 2},
]


def test_layout_route_passes_heading_level(env: None, tmp_path: Path) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()
    with TestClient(app) as client:
        _seed_authed(app, client, tmp_path / "docs", {"report.md": "# Report\n\nBody.\n"}, "alice@x")
        with session_scope() as s:
            f = s.execute(select(File).where(File.rel_path == "report.md")).scalar_one()
            file_id, cas_id = f.id, f.file_cas_id
        cas_store.write_file_blob(cas_id, "page_layout.json", json.dumps(_BLOCKS))

        r = client.get(f"/api/files/{file_id}/layout")
        assert r.status_code == 200, r.text
        assert r.json() == [
            {"page": 1, "type": "text", "text": "Annual Operating Review", "text_level": 2},
            {"page": 1, "type": "text", "text": "Body paragraph.", "text_level": None},
            {"page": 1, "type": "chart", "text": None, "text_level": None},
            # A bool is not a heading level.
            {"page": 2, "type": "text", "text": "odd", "text_level": None},
            {"page": 2, "type": "list", "text": None, "text_level": None},
        ]
