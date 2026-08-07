"""Clerk impersonation across named instances (admin/impersonation.py).

Fixes the "which Agnitio?" mixup: impersonation used a single key and bare
org names, so a Production-only org was unreachable and two same-named orgs
across instances were indistinguishable. It now shares the login
provisioning helper. These tests pin: prefixed labels, cross-instance
target selection, and login≡impersonation parity (the drift guard).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import auth_as
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.db.models import User
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc

_DIR_DEV = {
    "users": [{"id": "u_s", "email": "sagiv@agnitio.ai", "name": "Sagiv",
               "orgs": [{"id": "org_dev_agn", "name": "Agnitio"}]}],
    "organizations": [
        {"id": "org_dev_agn", "name": "Agnitio",
         "members": [{"email": "sagiv@agnitio.ai", "role": "member"}]},
    ],
}
_DIR_PROD = {
    "users": [{"id": "u_s", "email": "sagiv@agnitio.ai", "name": "Sagiv",
               "orgs": [{"id": "org_prod_agn", "name": "Agnitio"}]}],
    "organizations": [
        {"id": "org_prod_agn", "name": "Agnitio",
         "members": [{"email": "sagiv@agnitio.ai", "role": "member"}]},
    ],
}


@pytest.fixture
def super_admin_two_instances(app, monkeypatch):
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "boss@x")
    reset_settings_cache()
    uid = auth_as(app, "boss@x")
    with session_scope() as s:
        s.get(User, uid).is_admin = True
        s.commit()
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
    return app


def _accounts(email: str) -> dict[str, str]:
    with session_scope() as s:
        return {
            u.company_id: u.company_name
            for u in s.execute(select(User).where(User.email == email)).scalars()
        }


def test_impersonate_provisions_all_instances_prefixed(
    super_admin_two_instances: FastAPI,
) -> None:
    with TestClient(super_admin_two_instances) as c:
        r = c.post(
            "/api/admin/clerk/impersonate",
            json={"email": "sagiv@agnitio.ai", "company_id": "org_prod_agn"},
        )
        assert r.status_code == 200, r.text
        # Landed in the PRODUCTION Agnitio account (was unreachable before).
        assert r.json()["acting_as_email"] == "sagiv@agnitio.ai"

    accts = _accounts("sagiv@agnitio.ai")
    # Both instances' orgs provisioned, each with its prefixed label.
    assert accts["org_dev_agn"] == "Development / Agnitio"
    assert accts["org_prod_agn"] == "Production / Agnitio"


def test_impersonate_login_parity(super_admin_two_instances: FastAPI) -> None:
    """Impersonation and login must stamp identical company_name for an org
    — the drift guard. We provision via impersonation, then via the login
    helper directly, and require the same labels."""
    with TestClient(super_admin_two_instances) as c:
        c.post("/api/admin/clerk/impersonate", json={"email": "sagiv@agnitio.ai"})
    via_impersonation = _accounts("sagiv@agnitio.ai")

    # Now run the SHARED helper the login callback uses and compare.
    import asyncio

    from voitta_rag_enterprise.services.acl import (
        provision_accounts,
        resolve_clerk_admission,
    )

    adm = asyncio.run(resolve_clerk_admission("sagiv@agnitio.ai"))
    with session_scope() as s:
        provision_accounts(s, "sagiv@agnitio.ai", adm.display_name, adm.orgs)
        s.commit()
    via_login = _accounts("sagiv@agnitio.ai")

    assert via_impersonation == via_login
    assert via_login["org_prod_agn"] == "Production / Agnitio"


def test_impersonate_no_instances_400(app, monkeypatch) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "boss@x")
    reset_settings_cache()
    uid = auth_as(app, "boss@x")
    with session_scope() as s:
        s.get(User, uid).is_admin = True
        s.commit()
    with TestClient(app) as c:
        r = c.post("/api/admin/clerk/impersonate", json={"email": "x@y.co"})
        assert r.status_code == 400
