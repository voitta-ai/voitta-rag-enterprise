"""scripts/backfill_clerk_company_names.py — restamp bare company labels.

Pins: bare → "Instance / Org", idempotence, and that orgs in no enabled
instance are left untouched (never blank a label we can't re-derive).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scripts.backfill_clerk_company_names import _run
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import User
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc
from voitta_rag_enterprise.services.acl import get_or_create_user

_DIR_DEV = {
    "organizations": [{"id": "org_dev_agn", "name": "Agnitio"}],
    "users": [],
}
_DIR_PROD = {
    "organizations": [{"id": "org_prod_agn", "name": "Agnitio"}],
    "users": [],
}


@pytest.fixture
def two_instances(env: None, monkeypatch: pytest.MonkeyPatch):
    admin_store.save_clerk_instances(
        [
            {"name": "Development", "secret_key": "sk_test_dev", "enabled": True},
            {"name": "Production", "secret_key": "sk_live_prod", "enabled": True},
        ]
    )
    clerk_svc.clear_org_members_cache()

    async def fake_directory(secret_key: str) -> dict:
        return _DIR_DEV if secret_key == "sk_test_dev" else _DIR_PROD

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)


def _names() -> dict[str, str]:
    with session_scope() as s:
        return {
            u.company_id: u.company_name
            for u in s.execute(select(User)).scalars()
        }


def test_backfill_restamps_and_is_idempotent(two_instances) -> None:
    init_db()
    with session_scope() as s:
        get_or_create_user(s, "sagiv@x", "org_dev_agn", "Agnitio")   # bare
        get_or_create_user(s, "sagiv@x", "org_prod_agn", "Agnitio")  # bare
        get_or_create_user(s, "sagiv@x", "org_gone", "Legacy Co")    # unmapped
        get_or_create_user(s, "sagiv@x")                             # Personal
        s.commit()

    # Dry run writes nothing.
    assert _run(apply=False) == 0
    assert _names()["org_dev_agn"] == "Agnitio"

    # Apply restamps the two mapped orgs, leaves the rest.
    assert _run(apply=True) == 0
    names = _names()
    assert names["org_dev_agn"] == "Development / Agnitio"
    assert names["org_prod_agn"] == "Production / Agnitio"
    assert names["org_gone"] == "Legacy Co"   # in no enabled instance → untouched
    assert names[""] == ""                    # Personal untouched

    # Second apply is a no-op (idempotent) — nothing left to change.
    with session_scope() as s:
        before = {
            u.id: u.company_name for u in s.execute(select(User)).scalars()
        }
    _run(apply=True)
    with session_scope() as s:
        after = {
            u.id: u.company_name for u in s.execute(select(User)).scalars()
        }
    assert before == after


def test_backfill_no_instances_returns_error(env: None) -> None:
    init_db()
    assert _run(apply=True) == 1
