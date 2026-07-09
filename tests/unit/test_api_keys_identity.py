"""Contract tests for the token→identity seam (``identity_for_token``).

This is the single function MCP's BearerAuthMiddleware relies on to turn a
bearer token into ``(email, account_id)``. Future key-model changes (e.g.
org-scoped keys) will widen this contract — these tests pin today's shape.
"""

from __future__ import annotations

import time

from voitta_rag_enterprise.api.routes.api_keys import (
    identity_for_token,
    mint_token,
)
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import ApiKey
from voitta_rag_enterprise.services.acl import get_or_create_user


def _mint_for(email: str, company_id: str = "") -> tuple[str, int]:
    """Create an account + persisted key; return (plaintext_token, account_id)."""
    token, prefix, key_hash = mint_token()
    with session_scope() as s:
        user = get_or_create_user(s, email, company_id)
        s.add(
            ApiKey(
                user_id=user.id,
                name="t",
                prefix=prefix,
                key_hash=key_hash,
                created_at=int(time.time()),
            )
        )
        s.flush()
        account_id = user.id
    return token, account_id


def test_valid_token_resolves_to_minting_account(env: None) -> None:
    init_db()
    token, account_id = _mint_for("alice@x")
    with session_scope() as s:
        assert identity_for_token(s, token) == ("alice@x", account_id)


def test_key_is_the_account_selector(env: None) -> None:
    """A key minted under a company account resolves to THAT account id,
    not the Personal one — the key IS the account selector for MCP."""
    init_db()
    token, company_account_id = _mint_for("bob@x", company_id="org_123")
    with session_scope() as s:
        personal = get_or_create_user(s, "bob@x")  # distinct Personal row
        assert personal.id != company_account_id
    with session_scope() as s:
        assert identity_for_token(s, token) == ("bob@x", company_account_id)


def test_invalid_tokens_resolve_to_none(env: None) -> None:
    init_db()
    _mint_for("carol@x")
    with session_scope() as s:
        assert identity_for_token(s, "") is None
        assert identity_for_token(s, "garbage") is None
        assert identity_for_token(s, "vk_wrongwrongwrong") is None
        # Right entropy, wrong prefix → rejected before any DB lookup.
        assert identity_for_token(s, "xx_" + "a" * 43) is None


def test_valid_lookup_bumps_last_used_at(env: None) -> None:
    init_db()
    token, _ = _mint_for("dave@x")
    with session_scope() as s:
        row = s.query(ApiKey).one()
        assert row.last_used_at is None
    with session_scope() as s:
        identity_for_token(s, token)
    with session_scope() as s:
        assert s.query(ApiKey).one().last_used_at is not None


def test_orphaned_key_resolves_to_none(env: None) -> None:
    """Key whose account row is gone (defensive path) → None, not a crash."""
    init_db()
    token, account_id = _mint_for("eve@x")
    with session_scope() as s:
        from voitta_rag_enterprise.db.models import User

        s.delete(s.get(User, account_id))
    with session_scope() as s:
        assert identity_for_token(s, token) is None