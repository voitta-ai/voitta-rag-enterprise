"""Company API keys — the key+email→identity seam and the admin-gated routes.

A company key (``cvk_…``) is scoped to a Clerk org or the native space
(``company_id=''``) and must be paired with a user email on every request.
These tests pin the auth contract MCP's BearerAuthMiddleware relies on
(``resolve_company_identity``) and the scope-admin gate on the management
routes.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from voitta_rag_enterprise.api.routes.company_keys import (
    mint_company_token,
    resolve_company_identity,
    split_company_bearer,
)
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import CompanyApiKey, User
from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc
from voitta_rag_enterprise.services.acl import get_or_create_user

from ..conftest import auth_as


def _mint_company_key(company_id: str = "", company_name: str = "") -> str:
    """Persist a company key row; return the plaintext token."""
    token, prefix, key_hash = mint_company_token()
    with session_scope() as s:
        s.add(
            CompanyApiKey(
                company_id=company_id,
                company_name=company_name,
                name="t",
                prefix=prefix,
                key_hash=key_hash,
                created_by="admin@x",
                created_at=int(time.time()),
            )
        )
    return token


def _account_id(email: str, company_id: str = "") -> int | None:
    with session_scope() as s:
        row = s.execute(
            select(User.id).where(
                User.email == email, User.company_id == company_id
            )
        ).first()
        return row[0] if row else None


@pytest.fixture
def clerk_org(monkeypatch: pytest.MonkeyPatch):
    """Enable Clerk with a fake membership directory for org_1."""
    members = {"alice@x": "admin", "bob@x": "member"}
    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")

    async def fake_members(secret_key: str, org_id: str) -> dict[str, str]:
        assert org_id == "org_1"
        return members

    monkeypatch.setattr(clerk_svc, "fetch_org_members", fake_members)
    return members


# ---------------------------------------------------------------------------
# Bearer parsing
# ---------------------------------------------------------------------------


def test_split_company_bearer() -> None:
    assert split_company_bearer("cvk_abc") == ("cvk_abc", None)
    assert split_company_bearer("cvk_abc:a@x") == ("cvk_abc", "a@x")
    # Only the FIRST colon splits — token bodies never contain ':'.
    assert split_company_bearer("cvk_abc:a@x:oops") == ("cvk_abc", "a@x:oops")


# ---------------------------------------------------------------------------
# Native-space keys (company_id='')
# ---------------------------------------------------------------------------


async def test_native_key_admits_allowlisted_email_and_provisions(env: None) -> None:
    init_db()
    admin_store.add_allowed_user("carol@x")
    token = _mint_company_key()
    assert _account_id("carol@x") is None  # never signed in
    identity = await resolve_company_identity(token, "carol@x")
    assert identity is not None
    email, account_id = identity
    assert email == "carol@x"
    assert _account_id("carol@x") == account_id  # JIT-provisioned Personal row


async def test_native_key_rejects_unlisted_and_blocked(env: None) -> None:
    init_db()
    admin_store.add_allowed_user("ok@x")
    admin_store.add_allowed_user("blocked@x")
    admin_store.add_blocked_user("blocked@x")
    token = _mint_company_key()
    assert await resolve_company_identity(token, "stranger@x") is None
    assert await resolve_company_identity(token, "blocked@x") is None
    assert await resolve_company_identity(token, "ok@x") is not None


async def test_missing_email_or_bad_token_rejected(env: None) -> None:
    init_db()
    admin_store.add_allowed_user("ok@x")
    token = _mint_company_key()
    assert await resolve_company_identity(token, None) is None
    assert await resolve_company_identity(token, "not-an-email") is None
    assert await resolve_company_identity("cvk_wrong", "ok@x") is None


async def test_embedded_email_and_header_precedence(env: None) -> None:
    init_db()
    admin_store.add_allowed_user("a@x")
    admin_store.add_allowed_user("b@x")
    token = _mint_company_key()
    # Embedded form works…
    identity = await resolve_company_identity(f"{token}:a@x", None)
    assert identity is not None and identity[0] == "a@x"
    # …and the header wins when both are present.
    identity = await resolve_company_identity(f"{token}:a@x", "b@x")
    assert identity is not None and identity[0] == "b@x"


async def test_valid_use_bumps_last_used_at(env: None) -> None:
    init_db()
    admin_store.add_allowed_user("ok@x")
    token = _mint_company_key()
    with session_scope() as s:
        assert s.query(CompanyApiKey).one().last_used_at is None
    await resolve_company_identity(token, "ok@x")
    with session_scope() as s:
        assert s.query(CompanyApiKey).one().last_used_at is not None


async def test_email_is_case_insensitive(env: None) -> None:
    """Clients may send any casing; identity always lands on the lowercase
    account (same normalization as the OAuth login path)."""
    init_db()
    admin_store.add_allowed_user("ok@x")
    token = _mint_company_key()
    identity = await resolve_company_identity(token, "  OK@X ")
    assert identity is not None
    assert identity[0] == "ok@x"
    # Both casings resolve to the SAME account row — no duplicate users.
    identity2 = await resolve_company_identity(token, "ok@x")
    assert identity2 == identity


async def test_empty_header_falls_back_to_embedded_email(env: None) -> None:
    """An empty X-Voitta-User-Email header (present but blank) must not
    shadow the token-embedded email."""
    init_db()
    admin_store.add_allowed_user("a@x")
    token = _mint_company_key()
    identity = await resolve_company_identity(f"{token}:a@x", "")
    assert identity is not None and identity[0] == "a@x"


async def test_personal_key_never_resolves_as_company_key(env: None) -> None:
    """A valid PERSONAL key routed into the company resolver (or with an
    email bolted on) must not authenticate — the tables are disjoint."""
    from voitta_rag_enterprise.api.routes.api_keys import mint_token
    from voitta_rag_enterprise.db.models import ApiKey

    init_db()
    admin_store.add_allowed_user("ok@x")
    vk_token, prefix, key_hash = mint_token()
    with session_scope() as s:
        user = get_or_create_user(s, "ok@x")
        s.add(
            ApiKey(
                user_id=user.id,
                name="personal",
                prefix=prefix,
                key_hash=key_hash,
                created_at=int(time.time()),
            )
        )
    assert await resolve_company_identity(vk_token, "ok@x") is None
    assert await resolve_company_identity(f"{vk_token}:ok@x", None) is None


# ---------------------------------------------------------------------------
# Org-scoped keys (Clerk membership)
# ---------------------------------------------------------------------------


async def test_org_key_admits_member_and_provisions_org_account(
    env: None, clerk_org: dict
) -> None:
    init_db()
    token = _mint_company_key("org_1", "Acme")
    identity = await resolve_company_identity(token, "bob@x")
    assert identity is not None
    email, account_id = identity
    assert email == "bob@x"
    # The account is the ORG account, not Personal — the key selects scope.
    assert _account_id("bob@x", "org_1") == account_id
    with session_scope() as s:
        row = s.get(User, account_id)
        assert row.company_name == "Acme"


async def test_org_key_rejects_non_member_and_blocked_member(
    env: None, clerk_org: dict
) -> None:
    init_db()
    token = _mint_company_key("org_1", "Acme")
    assert await resolve_company_identity(token, "stranger@x") is None
    admin_store.add_blocked_user("bob@x")
    assert await resolve_company_identity(token, "bob@x") is None


async def test_org_key_clerk_outage_falls_back_to_existing_account(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_db()
    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")

    async def boom(secret_key: str, org_id: str) -> dict[str, str]:
        raise clerk_svc.ClerkError("down")

    monkeypatch.setattr(clerk_svc, "fetch_org_members", boom)
    token = _mint_company_key("org_1", "Acme")
    # bob has an existing (email, org) account from a past login → allowed.
    with session_scope() as s:
        get_or_create_user(s, "bob@x", "org_1", "Acme")
    assert await resolve_company_identity(token, "bob@x") is not None
    # carol has never been provisioned in this org → rejected.
    assert await resolve_company_identity(token, "carol@x") is None


async def test_org_key_clerk_disabled_uses_existing_account_fallback(
    env: None,
) -> None:
    """Clerk toggle off entirely (the `env` default): an org key still works
    for provisioned accounts and rejects unknown emails — same contract as
    an outage, so flipping the toggle can't widen access."""
    init_db()
    token = _mint_company_key("org_1", "Acme")
    with session_scope() as s:
        get_or_create_user(s, "bob@x", "org_1", "Acme")
    identity = await resolve_company_identity(token, "bob@x")
    assert identity is not None
    assert _account_id("bob@x", "org_1") == identity[1]
    assert await resolve_company_identity(token, "carol@x") is None
    # Native allowlisting does NOT leak into org scope: an allowlisted
    # email without an org account row is still rejected.
    admin_store.add_allowed_user("native-only@x")
    assert await resolve_company_identity(token, "native-only@x") is None


async def test_org_key_bumps_last_used_at(env: None, clerk_org: dict) -> None:
    init_db()
    token = _mint_company_key("org_1", "Acme")
    await resolve_company_identity(token, "bob@x")
    with session_scope() as s:
        assert s.query(CompanyApiKey).one().last_used_at is not None


# ---------------------------------------------------------------------------
# Management routes — scope-admin gate
# ---------------------------------------------------------------------------


def test_routes_403_for_non_admin(app: FastAPI, client: TestClient) -> None:
    auth_as(app, "user@x")
    assert client.get("/api/auth/company-keys").status_code == 403
    assert (
        client.post("/api/auth/company-keys", json={"name": "k"}).status_code == 403
    )
    assert client.delete("/api/auth/company-keys/1").status_code == 403


def test_native_admin_full_lifecycle(app: FastAPI, client: TestClient) -> None:
    uid = auth_as(app, "admin@x")
    with session_scope() as s:
        s.get(User, uid).is_admin = True

    created = client.post("/api/auth/company-keys", json={"name": "ci"})
    assert created.status_code == 200
    body = created.json()
    assert body["token"].startswith("cvk_")
    assert body["created_by"] == "admin@x"

    listed = client.get("/api/auth/company-keys").json()
    assert listed["company_id"] == ""  # Personal-active admin → native space
    assert [k["name"] for k in listed["keys"]] == ["ci"]
    # The plaintext token is never in the list payload.
    assert "token" not in listed["keys"][0]

    assert client.delete(
        f"/api/auth/company-keys/{body['id']}"
    ).status_code == 204
    assert client.get("/api/auth/company-keys").json()["keys"] == []


def test_clerk_org_admin_can_manage_own_org_only(
    app: FastAPI, client: TestClient, clerk_org: dict
) -> None:
    from voitta_rag_enterprise.api.deps import current_user, real_user
    from voitta_rag_enterprise.services.acl import CurrentUser

    with session_scope() as s:
        acc = get_or_create_user(s, "alice@x", "org_1", "Acme")
        member_acc = get_or_create_user(s, "bob@x", "org_1", "Acme")
        alice_id, bob_id = acc.id, member_acc.id

    # alice is org:admin in the fake directory → may manage org_1 keys.
    fake = lambda: CurrentUser(  # noqa: E731
        id=alice_id, email="alice@x", company_id="org_1", company_name="Acme"
    )
    app.dependency_overrides[current_user] = fake
    app.dependency_overrides[real_user] = fake
    created = client.post("/api/auth/company-keys", json={"name": "org key"})
    assert created.status_code == 200
    listed = client.get("/api/auth/company-keys").json()
    assert listed["company_id"] == "org_1"
    assert listed["company_name"] == "Acme"

    # bob is a plain member → 403 even inside his own org.
    fake_bob = lambda: CurrentUser(  # noqa: E731
        id=bob_id, email="bob@x", company_id="org_1", company_name="Acme"
    )
    app.dependency_overrides[current_user] = fake_bob
    app.dependency_overrides[real_user] = fake_bob
    assert client.get("/api/auth/company-keys").status_code == 403


def test_create_rejects_blank_name(app: FastAPI, client: TestClient) -> None:
    uid = auth_as(app, "admin@x")
    with session_scope() as s:
        s.get(User, uid).is_admin = True
    # Whitespace-only passes pydantic's min_length but must still 400.
    assert (
        client.post("/api/auth/company-keys", json={"name": "   "}).status_code
        == 400
    )
    # Empty string is rejected at the schema layer.
    assert (
        client.post("/api/auth/company-keys", json={"name": ""}).status_code == 422
    )


def test_native_admin_manages_org_scope_when_active(
    app: FastAPI, client: TestClient
) -> None:
    """Admin rights are person-level: a native admin who switches to a
    company account may mint keys for THAT org without any Clerk role."""
    from voitta_rag_enterprise.api.deps import current_user, real_user
    from voitta_rag_enterprise.services.acl import CurrentUser, stamp_person_admin

    with session_scope() as s:
        get_or_create_user(s, "admin@x")  # Personal anchor
        org_acc = get_or_create_user(s, "admin@x", "org_7", "Globex")
        stamp_person_admin(s, "admin@x", True)
        org_id = org_acc.id

    fake = lambda: CurrentUser(  # noqa: E731
        id=org_id, email="admin@x", company_id="org_7", company_name="Globex"
    )
    app.dependency_overrides[current_user] = fake
    app.dependency_overrides[real_user] = fake
    created = client.post("/api/auth/company-keys", json={"name": "org key"})
    assert created.status_code == 200
    listed = client.get("/api/auth/company-keys").json()
    assert listed["company_id"] == "org_7"
    with session_scope() as s:
        assert s.query(CompanyApiKey).one().company_id == "org_7"


def test_org_member_403_when_clerk_disabled(
    app: FastAPI, client: TestClient
) -> None:
    """No Clerk → no org-admin path: the gate fails CLOSED for a non-native
    admin on an org account instead of erroring or letting them through."""
    from voitta_rag_enterprise.api.deps import current_user, real_user
    from voitta_rag_enterprise.services.acl import CurrentUser

    with session_scope() as s:
        acc = get_or_create_user(s, "bob@x", "org_1", "Acme")
        bob_id = acc.id
    fake = lambda: CurrentUser(  # noqa: E731
        id=bob_id, email="bob@x", company_id="org_1", company_name="Acme"
    )
    app.dependency_overrides[current_user] = fake
    app.dependency_overrides[real_user] = fake
    assert client.get("/api/auth/company-keys").status_code == 403


def test_clerk_error_in_admin_gate_is_403_not_500(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clerk down while checking the caller's org role → clean 403."""
    from voitta_rag_enterprise.api.deps import current_user, real_user
    from voitta_rag_enterprise.services.acl import CurrentUser

    monkeypatch.setattr(admin_store, "get_clerk_enabled", lambda: True)
    monkeypatch.setattr(admin_store, "get_clerk_secret_key", lambda: "sk_test")

    async def boom(secret_key: str, org_id: str) -> dict[str, str]:
        raise clerk_svc.ClerkError("down")

    monkeypatch.setattr(clerk_svc, "fetch_org_members", boom)
    with session_scope() as s:
        acc = get_or_create_user(s, "alice@x", "org_1", "Acme")
        alice_id = acc.id
    fake = lambda: CurrentUser(  # noqa: E731
        id=alice_id, email="alice@x", company_id="org_1", company_name="Acme"
    )
    app.dependency_overrides[current_user] = fake
    app.dependency_overrides[real_user] = fake
    r = client.get("/api/auth/company-keys")
    assert r.status_code == 403
    assert "Clerk" in r.json()["detail"]


def test_delete_scoped_to_active_company(app: FastAPI, client: TestClient) -> None:
    """A native admin on their Personal account cannot delete an org key —
    scope comes from the ACTIVE account, and cross-scope ids read as 404."""
    uid = auth_as(app, "admin@x")
    with session_scope() as s:
        s.get(User, uid).is_admin = True
    _token, prefix, key_hash = mint_company_token()
    with session_scope() as s:
        s.add(
            CompanyApiKey(
                company_id="org_9",
                name="other org",
                prefix=prefix,
                key_hash=key_hash,
                created_at=int(time.time()),
            )
        )
        s.flush()
        key_id = s.query(CompanyApiKey).one().id
    assert client.delete(f"/api/auth/company-keys/{key_id}").status_code == 404


# ---------------------------------------------------------------------------
# Clerk membership TTL cache (services/clerk.fetch_org_members)
# ---------------------------------------------------------------------------


@pytest.fixture
def org_cache(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fresh cache + fake Clerk transport; returns the fetch-call log."""
    clerk_svc.clear_org_members_cache()
    calls: list[str] = []

    async def fake_paginated(client, path: str, secret_key: str) -> list[dict]:
        calls.append(path)
        return [
            {
                "public_user_data": {"user_id": "u1", "identifier": "Alice@X"},
                "role": "org:admin",
            },
            {
                "public_user_data": {"user_id": "u2", "identifier": "bob@x"},
                "role": "org:member",
            },
            {"public_user_data": {}, "role": "org:member"},  # no email → skipped
        ]

    monkeypatch.setattr(clerk_svc, "_get_paginated", fake_paginated)
    yield calls
    clerk_svc.clear_org_members_cache()


async def test_org_members_normalized_and_cached(env: None, org_cache: list) -> None:
    members = await clerk_svc.fetch_org_members("sk_a", "org_1")
    # Emails lowercased, "org:" role prefix stripped, empty emails dropped.
    assert members == {"alice@x": "admin", "bob@x": "member"}
    assert len(org_cache) == 1
    # Second call within the TTL is served from cache — no refetch.
    again = await clerk_svc.fetch_org_members("sk_a", "org_1")
    assert again == members
    assert len(org_cache) == 1


async def test_org_members_cache_keyed_by_secret_and_org(
    env: None, org_cache: list
) -> None:
    await clerk_svc.fetch_org_members("sk_a", "org_1")
    # Rotated secret key → refetch (stale members can't ride the old key).
    await clerk_svc.fetch_org_members("sk_b", "org_1")
    assert len(org_cache) == 2
    # Different org → its own entry.
    await clerk_svc.fetch_org_members("sk_a", "org_2")
    assert len(org_cache) == 3


async def test_org_members_cache_expires_after_ttl(
    env: None, org_cache: list
) -> None:
    await clerk_svc.fetch_org_members("sk_a", "org_1")
    # Rewind the stored timestamp past the TTL instead of sleeping/patching
    # the clock — the next call must hit Clerk again.
    for k, (ts, members) in list(clerk_svc._org_members_cache.items()):
        clerk_svc._org_members_cache[k] = (
            ts - clerk_svc.ORG_MEMBERS_TTL_S - 1,
            members,
        )
    await clerk_svc.fetch_org_members("sk_a", "org_1")
    assert len(org_cache) == 2


async def test_org_members_error_is_not_cached(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed fetch must not poison the cache — the next call retries."""
    clerk_svc.clear_org_members_cache()
    calls: list[int] = []

    async def flaky(client, path: str, secret_key: str) -> list[dict]:
        calls.append(1)
        if len(calls) == 1:
            raise clerk_svc.ClerkError("down")
        return [
            {
                "public_user_data": {"user_id": "u1", "identifier": "a@x"},
                "role": "org:member",
            }
        ]

    monkeypatch.setattr(clerk_svc, "_get_paginated", flaky)
    with pytest.raises(clerk_svc.ClerkError):
        await clerk_svc.fetch_org_members("sk_a", "org_1")
    assert await clerk_svc.fetch_org_members("sk_a", "org_1") == {"a@x": "member"}
    clerk_svc.clear_org_members_cache()
