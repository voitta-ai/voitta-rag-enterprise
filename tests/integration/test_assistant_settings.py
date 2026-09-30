"""Assistant configuration, credentials and conversation access (Phase 3).

The rules under test (services/assistant/identity.py + credentials.py):

- deployment credentials and policy: super-admins only;
- the Claude subscription: deployment-scoped, super-admins only, shared;
- personal API keys: any person, their own — keyed by email;
- the key that pays is always the REAL person's, never an impersonated one;
- secrets never come back from the API (masked hint only), are encrypted
  at rest, and a lost encryption key degrades to "not configured";
- conversations are reachable only from the owner's list or, while
  impersonating, the impersonated account's.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import auth_as
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import AssistantCredential
from voitta_rag_enterprise.services import secret_box
from voitta_rag_enterprise.services.acl import CurrentUser, get_or_create_user
from voitta_rag_enterprise.services.assistant import credentials, store
from voitta_rag_enterprise.services.assistant.identity import AssistantIdentity

API_KEY = "sk-ant-api03-" + "a" * 40
PERSONAL_KEY = "sk-ant-api03-" + "p" * 40
OAUTH = "sk-ant-oat01-" + "o" * 40


@pytest.fixture
def admin_app(auth_env: None, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.main import create_app

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "root@x")
    reset_settings_cache()
    return create_app()


def _user(email: str) -> CurrentUser:
    with session_scope() as s:
        u = get_or_create_user(s, email)
        return CurrentUser(id=u.id, email=u.email)


def test_admin_manages_deployment_credentials(admin_app: FastAPI) -> None:
    auth_as(admin_app, "root@x")
    with TestClient(admin_app) as c:
        r = c.put("/api/assistant/credentials/deployment/anthropic_api_key", json={"secret": API_KEY})
        assert r.status_code == 200, r.text
        assert r.json()["hint"] == "…" + API_KEY[-4:]
        assert API_KEY not in r.text
        r = c.put("/api/assistant/credentials/deployment/claude_oauth_token", json={"secret": OAUTH})
        assert r.status_code == 200, r.text

        cfg = c.get("/api/assistant/config").json()
        assert cfg["is_admin"] is True
        assert cfg["credentials"]["deployment_api_key"]["configured"] is True
        assert cfg["credentials"]["subscription"]["configured"] is True
        assert {e["id"]: e["available"] for e in cfg["engines"]} == {
            "anthropic_api": True,
            "claude_subscription": True,
        }
        assert API_KEY not in str(cfg) and OAUTH not in str(cfg)

    # Encrypted at rest.
    with session_scope() as s:
        for row in s.query(AssistantCredential).all():
            assert row.secret_enc not in (API_KEY, OAUTH)
            assert secret_box.decrypt(row.secret_enc) in (API_KEY, OAUTH)


def test_regular_user_limits(admin_app: FastAPI) -> None:
    auth_as(admin_app, "root@x")
    with TestClient(admin_app) as c:
        c.put("/api/assistant/credentials/deployment/claude_oauth_token", json={"secret": OAUTH})

    auth_as(admin_app, "bob@x")
    with TestClient(admin_app) as c:
        assert c.put(
            "/api/assistant/credentials/deployment/anthropic_api_key", json={"secret": API_KEY}
        ).status_code == 403
        assert c.put(
            "/api/assistant/credentials/person/claude_oauth_token", json={"secret": OAUTH}
        ).status_code == 403
        assert c.patch("/api/assistant/policy", json={"enabled": False}).status_code == 403
        assert c.put(
            "/api/assistant/credentials/person/anthropic_api_key", json={"secret": "nonsense"}
        ).status_code == 422
        assert c.put(
            "/api/assistant/credentials/person/anthropic_api_key", json={"secret": PERSONAL_KEY}
        ).status_code == 200

        cfg = c.get("/api/assistant/config").json()
        assert cfg["is_admin"] is False
        assert "subscription" not in cfg["credentials"]
        engines = {e["id"]: e for e in cfg["engines"]}
        assert engines["anthropic_api"]["available"] is True
        assert engines["claude_subscription"]["available"] is False
        assert "super-admins" in engines["claude_subscription"]["reason"]


def test_real_person_pays_under_impersonation(env: None) -> None:
    init_db()
    admin, bob = _user("root@x"), _user("bob@x")
    with session_scope() as s:
        credentials.store(s, AssistantIdentity(bob, bob), "person", "anthropic_api_key", PERSONAL_KEY)
        # Admin "views as" bob: bob's personal key must not be used.
        viewing = AssistantIdentity(real=admin, effective=bob)
        assert credentials.resolve(s, viewing, "anthropic_api") is None
        credentials.store(s, AssistantIdentity(admin, admin), "person", "anthropic_api_key", API_KEY)
        resolved = credentials.resolve(s, viewing, "anthropic_api")
        assert resolved is not None and resolved.secret == API_KEY and resolved.source == "person"


def test_env_key_is_deployment_fallback(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_ASSISTANT_API_KEY", API_KEY)
    reset_settings_cache()
    init_db()
    bob = _user("bob@x")
    with session_scope() as s:
        resolved = credentials.resolve(s, AssistantIdentity(bob, bob), "anthropic_api")
    assert resolved is not None and resolved.source == "environment"


def test_lost_encryption_key_degrades_to_unconfigured(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    from voitta_rag_enterprise.config import reset_settings_cache

    init_db()
    bob = _user("bob@x")
    ident = AssistantIdentity(bob, bob)
    with session_scope() as s:
        credentials.store(s, ident, "person", "anthropic_api_key", PERSONAL_KEY)
    monkeypatch.setenv("VOITTA_SECRET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    with session_scope() as s:
        assert credentials.resolve(s, ident, "anthropic_api") is None


def test_conversation_views_and_access(admin_app: FastAPI) -> None:
    from voitta_rag_enterprise.api.deps import current_user, real_user

    admin, bob, carol = _user("root@x"), _user("bob@x"), _user("carol@x")
    with session_scope() as s:
        mine = store.create_conversation(
            s, owner_user_id=admin.id, created_by_user_id=admin.id,
            engine="anthropic_api", model="claude-opus-5",
        ).id
        theirs = store.create_conversation(
            s, owner_user_id=bob.id, created_by_user_id=bob.id,
            engine="anthropic_api", model="claude-opus-5",
        ).id
        foreign = store.create_conversation(
            s, owner_user_id=carol.id, created_by_user_id=carol.id,
            engine="anthropic_api", model="claude-opus-5",
        ).id

    # Admin impersonating bob.
    admin_app.dependency_overrides[real_user] = lambda: admin
    admin_app.dependency_overrides[current_user] = lambda: bob
    with TestClient(admin_app) as c:
        assert [x["id"] for x in c.get("/api/assistant/conversations?view=mine").json()] == [mine]
        assert [x["id"] for x in c.get("/api/assistant/conversations?view=theirs").json()] == [theirs]
        assert c.get(f"/api/assistant/conversations/{theirs}").status_code == 200
        assert c.get(f"/api/assistant/conversations/{foreign}").status_code == 404
        assert c.delete(f"/api/assistant/conversations/{foreign}").status_code == 404

    # Bob alone sees only his own, and "theirs" is his own list too.
    auth_as(admin_app, "bob@x")
    with TestClient(admin_app) as c:
        assert [x["id"] for x in c.get("/api/assistant/conversations?view=theirs").json()] == [theirs]
        assert c.get(f"/api/assistant/conversations/{mine}").status_code == 404
        r = c.patch(f"/api/assistant/conversations/{theirs}", json={"title": "Q3 sync", "archived": True})
        assert r.json()["title"] == "Q3 sync" and r.json()["archived"] is True
        assert c.get("/api/assistant/conversations").json() == []
        assert len(c.get("/api/assistant/conversations?archived=true").json()) == 1


def test_policy_roundtrip(admin_app: FastAPI) -> None:
    auth_as(admin_app, "root@x")
    with TestClient(admin_app) as c:
        assert c.patch("/api/assistant/policy", json={"default_model": "gpt-9"}).status_code == 422
        r = c.patch(
            "/api/assistant/policy",
            json={"enabled": False, "default_model": "claude-sonnet-5", "default_effort": "low"},
        )
        assert r.json() == {"enabled": False, "default_model": "claude-sonnet-5", "default_effort": "low"}
        assert c.get("/api/assistant/config").json()["enabled"] is False
