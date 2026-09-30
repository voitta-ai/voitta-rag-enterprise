"""The assistant's read-only settings tools (services/settings_overview.py).

The rules under test:

- no secret ever reaches tool output — OAuth client secrets, Clerk keys,
  sync credential secrets, API key hashes and stored LLM credentials are
  planted everywhere a settings view reads, and must not appear;
- admin tools are offered only when the REAL person is an admin, and admin
  views are scoped exactly like the admin console (a regular admin sees
  only their companies' accounts and group members);
- impersonation: an admin viewing as someone keeps admin tools (their own
  rights) while account/folder tools describe the viewed account;
- folder share lists are owner-only; sync credentials stay inside the
  account's company.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import (
    ApiKey,
    AuthProvider,
    CompanyApiKey,
    Folder,
    SyncCredential,
)
from voitta_rag_enterprise.services import admin_store, groups
from voitta_rag_enterprise.services.acl import (
    CurrentUser,
    get_or_create_user,
    grant_folder,
    stamp_person_admin,
)
from voitta_rag_enterprise.services.admin_scope import AdminScope
from voitta_rag_enterprise.services.assistant import credentials
from voitta_rag_enterprise.services.assistant.identity import AssistantIdentity
from voitta_rag_enterprise.services.assistant.tools import (
    ADMIN_TOOLS,
    TOOLS,
    TOOLS_BY_NAME,
    ToolContext,
    tools_for,
)
from voitta_rag_enterprise.services.assistant.turns import _admin_scope_for
from voitta_rag_enterprise.services.retrieval import Viewer

SECRETS = {
    "oauth": "GOCSPX-provider-SECRET",
    "clerk": "sk_live_CLERK_SECRET_KEY",
    "sync_client": "sync-client-SECRET",
    "sync_refresh": "1//refresh-SECRET",
    "sync_sa": "-----BEGIN PRIVATE KEY-----SA-SECRET",
    "api_hash": "api-key-hash-SECRET",
    "company_hash": "company-key-hash-SECRET",
    "llm_key": "sk-ant-api03-" + "L" * 40,
    "llm_oauth": "sk-ant-oat01-" + "O" * 40,
}


@pytest.fixture
def world(env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "root@x")
    reset_settings_cache()
    init_db()
    now = int(time.time())
    admin_store.add_allowed_domain("acme.com")
    admin_store.save_clerk_instances(
        [{"name": "Production", "secret_key": SECRETS["clerk"], "enabled": False}]
    )
    with session_scope() as s:
        root = get_or_create_user(s, "root@x")
        stamp_person_admin(s, "root@x", True)
        bob = get_or_create_user(s, "bob@acme.com")
        carol = get_or_create_user(s, "carol@other.com")
        # Company accounts for the regular-admin scope test.
        a1 = get_or_create_user(s, "ann@acme.com", company_id="org_acme", company_name="Acme")
        o1 = get_or_create_user(s, "oli@other.com", company_id="org_other", company_name="Other")
        s.add(AuthProvider(
            provider="google", label="Google", client_id="cid.apps", client_secret=SECRETS["oauth"],
            tenant_id="", enabled=True, source="db", created_at=now, updated_at=now,
        ))
        s.add(SyncCredential(
            company_id="", kind="google_oauth_client", label="Drive app", client_id="drive.apps",
            client_secret=SECRETS["sync_client"], refresh_token=SECRETS["sync_refresh"],
            service_account_json=SECRETS["sync_sa"], connected_email="bob@acme.com",
            created_by="bob@acme.com", created_at=now, updated_at=now,
        ))
        s.add(SyncCredential(
            company_id="org_other", kind="google_service_account", label="Other SA",
            client_id="", created_by="oli@other.com", created_at=now, updated_at=now,
        ))
        s.add(ApiKey(user_id=bob.id, name="laptop", prefix="vk_ab", key_hash=SECRETS["api_hash"], created_at=now))
        s.add(CompanyApiKey(
            company_id="", company_name="", name="gateway", prefix="cvk_x",
            key_hash=SECRETS["company_hash"], created_by="root@x", created_at=now,
        ))
        folder = Folder(path=str(tmp_path / "specs"), display_name="Specs", owner_id=bob.id, shared=False)
        s.add(folder)
        s.flush()
        grant_folder(s, folder.id, carol.id)
        g = groups.get_or_create_group(s, "engineering")
        groups.add_member(s, g.id, a1.id)
        groups.add_member(s, g.id, o1.id)
        users = {u.email: CurrentUser(id=u.id, email=u.email, company_id=u.company_id,
                                      company_name=u.company_name)
                 for u in (root, bob, carol, a1, o1)}
        folder_id = folder.id
    root_ident = AssistantIdentity(users["root@x"], users["root@x"])
    with session_scope() as s:
        credentials.store(s, root_ident, "deployment", "anthropic_api_key", SECRETS["llm_key"])
        credentials.store(s, root_ident, "deployment", "claude_oauth_token", SECRETS["llm_oauth"])
    return {"users": users, "folder_id": folder_id}


def _ctx(real: CurrentUser, effective: CurrentUser, scope: AdminScope | None) -> ToolContext:
    return ToolContext(
        viewer=Viewer(user_id=effective.id, scope="visible"),
        real=real, effective=effective, admin_scope=scope,
    )


async def _call(ctx: ToolContext, name: str, args: dict | None = None) -> tuple[bool, object]:
    out = await TOOLS_BY_NAME[name].run(ctx, args or {})
    return out.is_error, json.loads(out.text)


async def test_no_secret_reaches_any_settings_tool(world: dict) -> None:
    root, bob = world["users"]["root@x"], world["users"]["bob@acme.com"]
    super_ctx = _ctx(root, root, AdminScope(is_super=True, is_native_admin=True))
    owner_ctx = _ctx(bob, bob, None)
    outputs = []
    for ctx, name, args in [
        (super_ctx, "admin_overview", {}),
        (super_ctx, "admin_users", {}),
        (super_ctx, "admin_groups", {"include_members": True}),
        (super_ctx, "my_account", {}),
        (super_ctx, "sync_credentials", {}),
        (owner_ctx, "my_account", {}),
        (owner_ctx, "folder_settings", {"folder_id": world["folder_id"]}),
        (owner_ctx, "sync_credentials", {}),
    ]:
        is_error, payload = await _call(ctx, name, args)
        assert not is_error, (name, payload)
        outputs.append(json.dumps(payload))
    blob = "\n".join(outputs)
    for label, secret in SECRETS.items():
        assert secret not in blob, f"{label} secret leaked"
    # The non-secret facts are there.
    assert "Drive app" in blob and "cid.apps" in blob and "laptop" in blob and "gateway" in blob


async def test_admin_tools_follow_the_real_person(world: dict) -> None:
    u = world["users"]
    root, bob = u["root@x"], u["bob@acme.com"]

    assert await _admin_scope_for(AssistantIdentity(bob, bob)) is None
    scope = await _admin_scope_for(AssistantIdentity(root, root))
    assert scope is not None and scope.is_super
    # Viewing as bob keeps the admin's own rights.
    viewing = AssistantIdentity(real=root, effective=bob)
    scope_viewing = await _admin_scope_for(viewing)
    assert scope_viewing is not None and scope_viewing.is_super

    assert tools_for(_ctx(bob, bob, None)) == TOOLS
    assert set(tools_for(_ctx(root, bob, scope_viewing))) == {*TOOLS, *ADMIN_TOOLS}

    # A non-admin can't reach an admin tool even by name.
    is_error, payload = await _call(_ctx(bob, bob, None), "admin_users")
    assert is_error and "admins only" in payload["error"]

    # Account view while impersonating: the person is root, the view is bob's.
    _err, account = await _call(_ctx(root, bob, scope_viewing), "my_account")
    assert account["impersonating"] is True
    assert account["person"]["email"] == "root@x" and account["person"]["is_super_admin"]
    assert account["active_account"]["email"] == "bob@acme.com"
    assert [k["name"] for k in account["api_keys"]] == ["laptop"]


async def test_regular_admin_sees_only_their_companies(world: dict) -> None:
    root = world["users"]["root@x"]
    scope = AdminScope(admin_org_ids=frozenset({"org_acme"}), admin_org_names=frozenset({"Acme"}))
    ctx = _ctx(root, root, scope)
    _err, users = await _call(ctx, "admin_users")
    assert [x["email"] for x in users["users"]] == ["ann@acme.com"]
    _err, filtered = await _call(ctx, "admin_users", {"query": "OTHER"})
    assert filtered["total"] == 0
    _err, grps = await _call(ctx, "admin_groups", {"include_members": True})
    (eng,) = grps
    assert eng["member_count"] == 2
    assert [m["email"] for m in eng["members"]] == ["ann@acme.com"]
    _err, overview = await _call(ctx, "admin_overview")
    assert overview["your_permissions"]["can_change_deployment_settings"] is False
    assert overview["your_permissions"]["administered_companies"] == ["Acme"]


async def test_folder_share_list_is_owner_only(world: dict) -> None:
    u = world["users"]
    bob, carol, oli = u["bob@acme.com"], u["carol@other.com"], u["oli@other.com"]
    fid = world["folder_id"]

    _err, owner_view = await _call(_ctx(bob, bob, None), "folder_settings", {"folder_id": fid})
    assert owner_view["owned_by_you"] is True
    assert [p["email"] for p in owner_view["sharing"]["people"]] == ["carol@other.com"]

    _err, grantee_view = await _call(_ctx(carol, carol, None), "folder_settings", {"folder_id": fid})
    assert grantee_view["owned_by_you"] is False
    assert grantee_view["sharing"] == "visible to the folder owner only"
    assert grantee_view["owner_email"] == "bob@acme.com"

    is_error, payload = await _call(_ctx(oli, oli, None), "folder_settings", {"folder_id": fid})
    assert is_error and "not found" in payload["error"]


async def test_sync_credentials_stay_in_the_company(world: dict) -> None:
    u = world["users"]
    _err, native = await _call(_ctx(u["bob@acme.com"], u["bob@acme.com"], None), "sync_credentials")
    assert [c["label"] for c in native] == ["Drive app"]
    assert native[0]["connected"] and native[0]["has_client_secret"]
    _err, other = await _call(_ctx(u["oli@other.com"], u["oli@other.com"], None), "sync_credentials")
    assert [c["label"] for c in other] == ["Other SA"]
