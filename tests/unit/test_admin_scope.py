"""Administrative-domain resolution + filtering (services/admin_scope.py).

Pins the scoping rules behind the admin-console fix: superadmins see all,
regular admins see only the Clerk orgs where they are an org admin plus the
native community (when native-allowed), and Clerk failures fail closed.
"""

from __future__ import annotations

import pytest

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.services import admin_scope as scope_mod
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc
from voitta_rag_enterprise.services.acl import get_or_create_user
from voitta_rag_enterprise.services.admin_scope import (
    AdminScope,
    admin_orgs_from_directory,
    filter_users_for_scope,
    resolve_admin_scope,
)

# Directory shape mirrors clerk.fetch_directory (roles already 'org:'-stripped).
_DIRECTORY = {
    "users": [
        {"id": "u_a", "email": "alice@x", "orgs": [{"id": "org_1", "name": "Acme"}]},
        {"id": "u_b", "email": "bob@x", "orgs": [{"id": "org_1", "name": "Acme"}]},
        {"id": "u_c", "email": "carol@x", "orgs": [{"id": "org_2", "name": "Beta"}]},
    ],
    "organizations": [
        {
            "id": "org_1", "name": "Acme",
            "members": [
                {"email": "alice@x", "role": "admin"},
                {"email": "bob@x", "role": "member"},
            ],
        },
        {
            "id": "org_2", "name": "Beta",
            "members": [{"email": "carol@x", "role": "admin"}],
        },
    ],
}


@pytest.fixture
def clerk_dir(monkeypatch: pytest.MonkeyPatch):
    """Clerk enabled with the fake directory above; fresh scope cache."""
    scope_mod.clear_directory_cache()
    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")

    async def fake_directory(secret_key: str) -> dict:
        return _DIRECTORY

    monkeypatch.setattr(clerk_svc, "fetch_directory", fake_directory)
    return _DIRECTORY


# ---------------------------------------------------------------------------
# admin_orgs_from_directory — pure
# ---------------------------------------------------------------------------


def test_admin_orgs_from_directory() -> None:
    ids, names = admin_orgs_from_directory(_DIRECTORY, "alice@x")
    assert ids == frozenset({"org_1"}) and names == frozenset({"Acme"})
    # A member (non-admin) administers nothing.
    assert admin_orgs_from_directory(_DIRECTORY, "bob@x") == (frozenset(), frozenset())
    # Case-insensitive.
    assert admin_orgs_from_directory(_DIRECTORY, "ALICE@X")[0] == frozenset({"org_1"})
    # Unknown email.
    assert admin_orgs_from_directory(_DIRECTORY, "nobody@x") == (frozenset(), frozenset())


# ---------------------------------------------------------------------------
# resolve_admin_scope
# ---------------------------------------------------------------------------


async def test_super_admin_sees_all(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    monkeypatch.setattr(admin_store, "is_super_admin", lambda e: e == "root@x")
    with session_scope() as s:
        scope = await resolve_admin_scope(s, "root@x")
    assert scope.is_super and scope.is_native_admin


async def test_org_admin_scope(env: None, clerk_dir: dict) -> None:
    init_db()
    with session_scope() as s:
        scope = await resolve_admin_scope(s, "alice@x")
    assert not scope.is_super
    assert scope.admin_org_ids == frozenset({"org_1"})
    assert scope.admin_org_names == frozenset({"Acme"})
    assert not scope.clerk_degraded


async def test_org_member_has_no_org_domain(env: None, clerk_dir: dict) -> None:
    init_db()
    with session_scope() as s:
        scope = await resolve_admin_scope(s, "bob@x")  # member, not admin
    assert scope.admin_org_ids == frozenset()


async def test_native_admin_flag(env: None, clerk_dir: dict) -> None:
    init_db()
    admin_store.add_allowed_user("nate@x")
    with session_scope() as s:
        scope = await resolve_admin_scope(s, "nate@x")
    assert scope.is_native_admin
    assert scope.admin_org_ids == frozenset()  # native but no org-admin role


async def test_clerk_error_fails_closed(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_db()
    scope_mod.clear_directory_cache()
    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")

    async def boom(secret_key: str) -> dict:
        raise clerk_svc.ClerkError("down")

    monkeypatch.setattr(clerk_svc, "fetch_directory", boom)
    with session_scope() as s:
        scope = await resolve_admin_scope(s, "alice@x")
    assert scope.admin_org_ids == frozenset() and scope.clerk_degraded


async def test_clerk_disabled_empty_org_domain(env: None) -> None:
    init_db()
    # env default: Clerk off. A non-super, non-native admin → empty domain.
    with session_scope() as s:
        scope = await resolve_admin_scope(s, "someone@x")
    assert not scope.is_super
    assert scope.admin_org_ids == frozenset()
    assert not scope.clerk_degraded


# ---------------------------------------------------------------------------
# user_in_scope / filter_users_for_scope
# ---------------------------------------------------------------------------


def _mk_user(email: str, company_id: str = "") -> None:
    with session_scope() as s:
        get_or_create_user(s, email, company_id)


def test_filtering_matrix(env: None) -> None:
    init_db()
    _mk_user("alice@x", "org_1")   # in an admin's org
    _mk_user("carol@x", "org_2")   # other org
    admin_store.add_allowed_user("nate@x")
    _mk_user("nate@x")             # native community, Personal account

    from sqlalchemy import select

    from voitta_rag_enterprise.db.models import User

    with session_scope() as s:
        all_users = list(s.execute(select(User)).scalars())

        # Super → everyone.
        assert len(filter_users_for_scope(AdminScope(is_super=True), all_users)) == len(all_users)

        # Org admin of org_1 (not native) → only org_1 accounts.
        org_scope = AdminScope(admin_org_ids=frozenset({"org_1"}))
        got = {u.email for u in filter_users_for_scope(org_scope, all_users)}
        assert got == {"alice@x"}

        # Native admin → native-community users, no orgs.
        nat_scope = AdminScope(is_native_admin=True)
        got = {u.email for u in filter_users_for_scope(nat_scope, all_users)}
        assert "nate@x" in got and "carol@x" not in got and "alice@x" not in got

        # Combined org + native.
        both = AdminScope(admin_org_ids=frozenset({"org_1"}), is_native_admin=True)
        got = {u.email for u in filter_users_for_scope(both, all_users)}
        assert got == {"alice@x", "nate@x"}

        # Empty domain → nothing.
        assert filter_users_for_scope(AdminScope(), all_users) == []


async def test_directory_cache_reuses_within_ttl(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second resolve within the TTL doesn't re-hit Clerk."""
    init_db()
    scope_mod.clear_directory_cache()
    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")
    calls = {"n": 0}

    async def counting(secret_key: str) -> dict:
        calls["n"] += 1
        return _DIRECTORY

    monkeypatch.setattr(clerk_svc, "fetch_directory", counting)
    with session_scope() as s:
        await resolve_admin_scope(s, "alice@x")
        await resolve_admin_scope(s, "bob@x")
    assert calls["n"] == 1
