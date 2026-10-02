"""API-key ``last_used_at`` bookkeeping must never fail (or even lock) a request.

Regression: every bearer-authenticated request wrote ``last_used_at``, so a busy
indexer holding the SQLite write lock turned plain GETs into 500s
("database is locked"). The write is now throttled and failure-tolerant.
"""

from __future__ import annotations

import contextlib
import time

from sqlalchemy.exc import OperationalError

from voitta_rag_enterprise.api.routes import company_keys
from voitta_rag_enterprise.api.routes.api_keys import (
    LAST_USED_GRANULARITY_S,
    commit_best_effort,
    last_used_is_stale,
    mint_token,
    verify_token,
)
from voitta_rag_enterprise.api.routes.company_keys import (
    mint_company_token,
    resolve_company_identity,
)
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import ApiKey, CompanyApiKey, User
from voitta_rag_enterprise.services import admin_store


def _locked() -> OperationalError:
    return OperationalError("UPDATE ...", {}, Exception("database is locked"))


def test_staleness_rule():
    now = 10_000
    assert last_used_is_stale(None, now)
    assert not last_used_is_stale(now - 1, now)
    assert not last_used_is_stale(now - LAST_USED_GRANULARITY_S + 1, now)
    assert last_used_is_stale(now - LAST_USED_GRANULARITY_S, now)


def test_commit_best_effort_swallows_lock_and_rolls_back():
    class FakeSession:
        rolled_back = False

        def commit(self):
            raise _locked()

        def rollback(self):
            self.rolled_back = True

    s = FakeSession()
    commit_best_effort(s)  # must not raise
    assert s.rolled_back


def test_commit_best_effort_still_raises_other_errors():
    class Boom:
        def commit(self):
            raise RuntimeError("real bug")

        def rollback(self):  # pragma: no cover
            pass

    try:
        commit_best_effort(Boom())
    except RuntimeError:
        return
    raise AssertionError("non-lock errors must propagate")


def test_verify_token_does_not_rewrite_fresh_last_used(env: None) -> None:
    init_db()
    with session_scope() as s:
        user = User(email="a@x", company_id="", company_name="")
        s.add(user)
        s.flush()
        token, prefix, key_hash = mint_token()
        s.add(ApiKey(user_id=user.id, name="k", prefix=prefix, key_hash=key_hash))

    with session_scope() as s:
        first = verify_token(s, token).last_used_at
    assert first is not None

    with session_scope() as s:
        row = verify_token(s, token)
        assert row.last_used_at == first
        assert not s.dirty  # nothing to flush => no write lock taken

    # Once stale, it is refreshed again.
    with session_scope() as s:
        s.get(ApiKey, row.id).last_used_at = int(time.time()) - 3600
    with session_scope() as s:
        assert verify_token(s, token).last_used_at >= first


async def test_company_key_auth_survives_locked_db_on_bump(
    env: None, monkeypatch
) -> None:
    init_db()
    admin_store.add_allowed_user("ok@x")
    token, prefix, key_hash = mint_company_token()
    with session_scope() as s:
        s.add(
            CompanyApiKey(
                company_id="", company_name="", name="t", prefix=prefix,
                key_hash=key_hash, created_by="admin@x", created_at=int(time.time()),
            )
        )

    real = company_keys.session_scope
    calls = {"n": 0}

    @contextlib.contextmanager
    def flaky():
        calls["n"] += 1
        if calls["n"] == 3:  # 1=key lookup, 2=get_or_create_user, 3=last_used bump
            raise _locked()
        with real() as s:
            yield s

    monkeypatch.setattr(company_keys, "session_scope", flaky)
    identity = await resolve_company_identity(token, "ok@x")
    assert identity is not None and identity[0] == "ok@x"
    assert calls["n"] == 3  # the bump WAS attempted, and its failure was tolerated
    with session_scope() as s:
        assert s.query(CompanyApiKey).one().last_used_at is None  # retried next time


async def test_company_key_fresh_last_used_causes_no_write(env: None, monkeypatch) -> None:
    init_db()
    admin_store.add_allowed_user("ok@x")
    token, prefix, key_hash = mint_company_token()
    with session_scope() as s:
        s.add(
            CompanyApiKey(
                company_id="", company_name="", name="t", prefix=prefix,
                key_hash=key_hash, created_by="admin@x", created_at=int(time.time()),
                last_used_at=int(time.time()),
            )
        )
    real = company_keys.session_scope
    calls = {"n": 0}

    @contextlib.contextmanager
    def counting():
        calls["n"] += 1
        with real() as s:
            yield s

    monkeypatch.setattr(company_keys, "session_scope", counting)
    assert await resolve_company_identity(token, "ok@x") is not None
    assert calls["n"] == 2  # lookup + get_or_create only; no bump transaction
