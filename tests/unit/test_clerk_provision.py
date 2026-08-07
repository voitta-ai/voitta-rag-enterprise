"""Shared Clerk admission + provisioning (services/acl/clerk_provision.py).

Pins the fix for the "which Agnitio?" bug: login and impersonation must
resolve a Clerk email through the SAME multi-instance path and stamp the
SAME ``"Instance / Org"`` labels, so two orgs that share a name across
instances stay distinguishable and no instance's orgs are ever missed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import User
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc
from voitta_rag_enterprise.services.acl import (
    provision_accounts,
    resolve_clerk_admission,
)

# Two instances, each with an "Agnitio" org — DISTINCT ids. The exact shape
# behind the reported mixup.
_DIR_DEV = {
    "users": [
        {"id": "u_s", "email": "sagiv@x", "name": "Sagiv",
         "orgs": [{"id": "org_dev_agn", "name": "Agnitio"},
                  {"id": "org_dev_won", "name": "Wonder"}]},
    ],
    "organizations": [],
}
_DIR_PROD = {
    "users": [
        {"id": "u_s", "email": "sagiv@x", "name": "Sagiv",
         "orgs": [{"id": "org_prod_agn", "name": "Agnitio"}]},
    ],
    "organizations": [],
}


@pytest.fixture
def two_instances(env: None, monkeypatch: pytest.MonkeyPatch):
    """Development + Production both enabled, each with its own directory."""
    admin_store.save_clerk_instances(
        [
            {"name": "Development", "secret_key": "sk_test_dev", "enabled": True},
            {"name": "Production", "secret_key": "sk_live_prod", "enabled": True},
        ]
    )
    clerk_svc.clear_org_members_cache()

    async def fake_directory(secret_key: str) -> dict:
        if secret_key == "sk_test_dev":
            return _DIR_DEV
        if secret_key == "sk_live_prod":
            return _DIR_PROD
        raise clerk_svc.ClerkError("unknown key")

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)


async def test_admission_unions_and_prefixes(two_instances) -> None:
    adm = await resolve_clerk_admission("sagiv@x")
    assert adm.matched
    assert adm.display_name == "Sagiv"
    by_id = {o["id"]: o["name"] for o in adm.orgs}
    # Same "Agnitio" name in both instances → DISTINCT ids, DISTINCT labels.
    assert by_id == {
        "org_dev_agn": "Development / Agnitio",
        "org_dev_won": "Development / Wonder",
        "org_prod_agn": "Production / Agnitio",
    }


async def test_provision_writes_prefixed_names(two_instances) -> None:
    init_db()
    adm = await resolve_clerk_admission("sagiv@x")
    with session_scope() as s:
        by_company = provision_accounts(s, "sagiv@x", adm.display_name, adm.orgs)
        assert set(by_company) == {
            "", "org_dev_agn", "org_dev_won", "org_prod_agn",
        }
        s.commit()
        names = {
            u.company_id: u.company_name
            for u in s.execute(
                select(User).where(User.email == "sagiv@x")
            ).scalars()
        }
    assert names["org_dev_agn"] == "Development / Agnitio"
    assert names["org_prod_agn"] == "Production / Agnitio"
    assert names[""] == ""  # Personal has no company label


async def test_provision_restamps_bare_name(two_instances) -> None:
    """A pre-existing bare-named row self-heals to the prefixed label."""
    init_db()
    from voitta_rag_enterprise.services.acl import get_or_create_user

    with session_scope() as s:
        get_or_create_user(s, "sagiv@x", "org_dev_agn", "Agnitio")  # bare
        s.commit()
    adm = await resolve_clerk_admission("sagiv@x")
    with session_scope() as s:
        provision_accounts(s, "sagiv@x", adm.display_name, adm.orgs)
        s.commit()
        row = s.execute(
            select(User).where(
                User.email == "sagiv@x", User.company_id == "org_dev_agn"
            )
        ).scalar_one()
        assert row.company_name == "Development / Agnitio"


async def test_admission_fail_soft_per_instance(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One instance down → the other's orgs still resolve."""
    admin_store.save_clerk_instances(
        [
            {"name": "Development", "secret_key": "sk_test_dev", "enabled": True},
            {"name": "Production", "secret_key": "sk_live_prod", "enabled": True},
        ]
    )
    clerk_svc.clear_org_members_cache()

    async def flaky(secret_key: str) -> dict:
        if secret_key == "sk_live_prod":
            raise clerk_svc.ClerkError("prod down")
        return _DIR_DEV

    monkeypatch.setattr(clerk_svc, "fetch_directory", flaky)
    adm = await resolve_clerk_admission("sagiv@x")
    assert adm.matched
    assert {o["id"] for o in adm.orgs} == {"org_dev_agn", "org_dev_won"}


async def test_no_instances_no_match(env: None) -> None:
    adm = await resolve_clerk_admission("sagiv@x")
    assert not adm.matched and adm.orgs == []
